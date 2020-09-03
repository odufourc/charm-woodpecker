#!/usr/bin/env python3

import socket
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.append('lib')

from ops.framework import (
    StoredState,
)
from ops.main import main
import ops.model
import charmhelpers.core.host as ch_host
import charmhelpers.core.templating as ch_templating
import interface_ceph_client.ceph_client as ceph_client
import interface_tls_certificates.ca_client as ca_client
import interface_ceph_benchmarking_peer

import bench_tools

import ops_openstack.adapters
import ops_openstack.core
import cryptography.hazmat.primitives.serialization as serialization
logger = logging.getLogger(__name__)


class CephClientAdapter(ops_openstack.adapters.OpenStackOperRelationAdapter):

    def __init__(self, relation):
        super(CephClientAdapter, self).__init__(relation)

    @property
    def mon_hosts(self):
        hosts = self.relation.get_relation_data()['mon_hosts']
        return ' '.join(sorted(hosts))

    @property
    def auth_supported(self):
        return self.relation.get_relation_data()['auth']

    @property
    def key(self):
        return self.relation.get_relation_data()['key']


class PeerAdapter(ops_openstack.adapters.OpenStackOperRelationAdapter):

    def __init__(self, relation):
        super(PeerAdapter, self).__init__(relation)


class GatewayClientPeerAdapter(PeerAdapter):

    def __init__(self, relation):
        super(GatewayClientPeerAdapter, self).__init__(relation)

    @property
    def gw_hosts(self):
        hosts = self.relation.peer_addresses
        return ' '.join(sorted(hosts))

    @property
    def trusted_ips(self):
        ips = self.allowed_ips
        ips.extend(self.relation.peer_addresses)
        return ' '.join(sorted(ips))


class TLSCertificatesAdapter(
        ops_openstack.adapters.OpenStackOperRelationAdapter):

    def __init__(self, relation):
        super(TLSCertificatesAdapter, self).__init__(relation)

    @property
    def enable_tls(self):
        try:
            return bool(self.relation.application_certificate)
        except ca_client.CAClientError:
            return False


class CephBenchmarkingAdapters(
        ops_openstack.adapters.OpenStackRelationAdapters):

    relation_adapters = {
        'ceph-client': CephClientAdapter,
        'cluster': GatewayClientPeerAdapter,
        'certificates': TLSCertificatesAdapter,
    }


class CephBenchmarkingCharmBase(ops_openstack.core.OSBaseCharm):

    state = StoredState()
    PACKAGES = ['ceph-common', 'fio']
    CEPH_CAPABILITIES = [
        "osd", "allow *",
        "mon", "allow *",
        "mgr", "allow *"]
    CLIENT_NAME = "client.ceph-benchmarking"

    REQUIRED_RELATIONS = ['ceph-client']

    CEPH_CONFIG_PATH = Path('/etc/ceph')
    RBD_FIO = CEPH_CONFIG_PATH / 'rbd.fio'
    CEPH_CONF = CEPH_CONFIG_PATH / 'ceph.conf'
    BENCHMARK_KEYRING = (
        CEPH_CONFIG_PATH / 'ceph.client.ceph-benchmarking.keyring')
    TLS_KEY_PATH = CEPH_CONFIG_PATH / 'ceph-benchmarking.key'
    TLS_PUB_KEY_PATH = CEPH_CONFIG_PATH / 'ceph-benchmarking-pub.key'
    TLS_CERT_PATH = CEPH_CONFIG_PATH / 'ceph-benchmarking.crt'
    TLS_KEY_AND_CERT_PATH = CEPH_CONFIG_PATH / 'ceph-benchmarking.pem'
    TLS_CA_CERT_PATH = Path(
        '/usr/local/share/ca-certificates/vault_ca_cert.crt')

    # We have no services to restart but using that verbiage to remain
    # consistent with rendering patterns in other charms.
    BENCHMARK_SERVICES = []
    RESTART_MAP = {
        str(RBD_FIO): BENCHMARK_SERVICES,
        str(CEPH_CONF): BENCHMARK_SERVICES,
        str(BENCHMARK_KEYRING): BENCHMARK_SERVICES}

    release = 'default'

    def __init__(self, framework):
        super().__init__(framework)
        logging.info("Using {} class".format(self.release))
        self.state.set_default(
            target_created=False,
            enable_tls=False)
        self.ceph_client = ceph_client.CephClientRequires(
            self,
            'ceph-client')
        self.peers = interface_ceph_benchmarking_peer.CephBenchmarkingPeers(
            self,
            'cluster')
        self.ca_client = ca_client.CAClient(
            self,
            'certificates')
        self.adapters = CephBenchmarkingAdapters(
            (self.ceph_client, self.peers, self.ca_client),
            self)
        self.framework.observe(
            self.ceph_client.on.broker_available,
            self.request_ceph_pool)
        self.framework.observe(
            self.ceph_client.on.pools_available,
            self.render_config)
        self.framework.observe(
            self.peers.on.has_peers,
            self.on_has_peers)
        self.framework.observe(
            self.peers.on.allowed_ips_changed,
            self.render_config)
        self.framework.observe(
            self.ca_client.on.tls_app_config_ready,
            self.on_tls_app_config_ready)
        self.framework.observe(
            self.ca_client.on.ca_available,
            self.on_ca_available)
        self.framework.observe(
            self.on.config_changed,
            self.render_config)
        self.framework.observe(
            self.on.upgrade_charm,
            self.render_config)
        self.framework.observe(
            self.on.rados_bench_action,
            self.on_rados_bench_action)

    def on_install(self, event):
        if ch_host.is_container():
            logging.info("Installing into a container is not supported")
            self.update_status()
        else:
            self.install_pkgs()

    def on_has_peers(self, event):
        # TODO: We may or may not have tasks as we go to multiple units
        # multiple units would allow for simultaneous stress tests against ceph
        logging.info("Unit has peers")
        pass

    def request_ceph_pool(self, event):
        logging.info("Requesting replicated pool")
        self.ceph_client.create_replicated_pool(
            self.model.config['pool-name'])
        logging.info("Requesting permissions")
        self.ceph_client.request_ceph_permissions(
            self.CLIENT_NAME,
            self.CEPH_CAPABILITIES)
        self.ceph_client.request_osd_settings({
            'osd heartbeat grace': 20,
            'osd heartbeat interval': 5})

    def refresh_request(self, event):
        self.render_config(event)
        self.request_ceph_pool(event)

    def render_config(self, event):
        if not self.ceph_client.pools_available:
            print("Defering setup pools")
            logging.info("Defering setup")
            event.defer()
            return

        self.CEPH_CONFIG_PATH.mkdir(
            exist_ok=True,
            mode=0o750)

        def _render_configs():
            for config_file in self.RESTART_MAP.keys():
                ch_templating.render(
                    os.path.basename(config_file),
                    config_file,
                    self.adapters)
        logging.info("Rendering config")
        _render_configs()
        logging.info("Setting started state")
        self.peers.announce_ready()
        self.state.is_started = True
        self.update_status()
        logging.info("on_pools_available: status updated")

    def on_ca_available(self, event):
        addresses = set()
        for binding_name in ['public', 'cluster']:
            binding = self.model.get_binding(binding_name)
            addresses.add(binding.network.ingress_address)
            addresses.add(binding.network.bind_address)
        sans = [str(s) for s in addresses]
        sans.append(socket.gethostname())
        self.ca_client.request_application_certificate(socket.getfqdn(), sans)

    def on_tls_app_config_ready(self, event):
        self.TLS_KEY_PATH.write_bytes(
            self.ca_client.application_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()))
        self.TLS_CERT_PATH.write_bytes(
            self.ca_client.application_certificate.public_bytes(
                encoding=serialization.Encoding.PEM))
        self.TLS_CA_CERT_PATH.write_bytes(
            self.ca_client.ca_certificate.public_bytes(
                encoding=serialization.Encoding.PEM))
        self.TLS_KEY_AND_CERT_PATH.write_bytes(
            self.ca_client.application_certificate.public_bytes(
                encoding=serialization.Encoding.PEM) +
            b'\n' +
            self.ca_client.application_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption())
        )
        self.TLS_PUB_KEY_PATH.write_bytes(
            self.ca_client.application_key.public_key().public_bytes(
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
                encoding=serialization.Encoding.PEM))
        subprocess.check_call(['update-ca-certificates'])
        self.state.enable_tls = True
        self.render_config(event)

    def custom_status_check(self):
        if ch_host.is_container():
            self.unit.status = ops.model.BlockedStatus(
                "Some charm actions cannot be performed when deployed in a "
                "container")
            return False
        return True

    # Actions
    def on_rados_bench_action(self, event):
        _pool_name = (
            event.params.get('pool-name') or
            self.model.config['pool-name'])
        _bench = bench_tools.BenchTools()
        try:
            _result = _bench.rados_bench(
                _pool_name,
                event.params['seconds'],
                event.params['operation'],
                client=self.CLIENT_NAME,
                switches=event.params.get('switches'))
            event.set_results({"output": _result})
        except subprocess.CalledProcessError as e:
            _msg = ("rados bench failed: {}"
                    .format(e.stderr.decode("UTF-8")))
            logging.error(_msg)
            event.set_failed({"Stderr": _msg})


@ops_openstack.core.charm_class
class CephBenchmarkingCharmJewel(CephBenchmarkingCharmBase):

    state = StoredState()
    release = 'jewel'


@ops_openstack.core.charm_class
class CephBenchmarkingCharmOcto(CephBenchmarkingCharmBase):

    state = StoredState()
    release = 'octopus'


if __name__ == '__main__':
    main(ops_openstack.core.get_charm_class_for_release())