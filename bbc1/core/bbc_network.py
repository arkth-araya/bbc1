# -*- coding: utf-8 -*-
"""
Copyright (c) 2017 beyond-blockchain.org.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from gevent import monkey
monkey.patch_all()
import gevent
import socket
import select

import threading
import random
import binascii
import struct
import time

import os
import sys
sys.path.extend(["../../", os.path.abspath(os.path.dirname(__file__))])
from bbc1.core import bbc_core
from bbc1.core.bbc_config import DEFAULT_P2P_PORT
from bbc1.core.bbc_ledger import ResourceType
from bbc1.common.bbclib import NodeInfo, StorageType
from bbc1.common import bbclib, message_key_types
from bbc1.common.message_key_types import to_2byte, PayloadType, KeyType
from bbc1.common import logger
from bbc1.common.bbc_error import *
from bbc1.core import query_management

TCP_THRESHOLD_SIZE = 1300
ZEROS = bytes([0] * 32)
NUM_CROSS_REF_COPY = 2

DURATION_GIVEUP_PUT = 30
INTERVAL_RETRY = 3
GET_RETRY_COUNT = 5
ROUTE_RETRY_COUNT = 1
REFRESH_INTERVAL = 1800  # not sure whether it's appropriate
ALIVE_CHECK_PING_WAIT = 2

ticker = query_management.get_ticker()


def check_my_IPaddresses(target4='8.8.8.8', target6='2001:4860:4860::8888', port=80):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target4, port))
        ip4 = s.getsockname()[0]
        s.close()
    except OSError:
        #ip4 = ""
        ip4 = "127.0.0.1"
    ip6 = ""
    if socket.has_ipv6:
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            s.connect((target6, port))
            ip6 = s.getsockname()[0]
            s.close()
        except OSError:
            ip6 = ""
    return ip4, ip6


def send_data_by_tcp(ipv4="", ipv6="", port=DEFAULT_P2P_PORT, msg=None):
    def worker():
        if ipv4 != "":
            conn = socket.create_connection((ipv4, port))
        elif ipv6 != "":
            conn = socket.create_connection((ipv6, port))
        conn.send(msg)
        conn.close()
    gevent.spawn(worker)


class BBcNetwork:
    """
    Socket and thread management for infrastructure layers
    """
    def __init__(self, config, core=None, p2p_port=None, use_global=True,
                 loglevel="all", logname=None):
        self.core = core
        self.logger = logger.get_logger(key="bbc_network", level=loglevel, logname=logname)
        self.logname = logname
        self.config = config
        self.use_global = use_global
        conf = self.config.get_config()
        self.domains = dict()
        self.asset_groups_to_advertise = set()
        self.ip_address, self.ip6_address = check_my_IPaddresses()
        if p2p_port is not None:
            conf['network']['p2p_port'] = p2p_port
            self.config.update_config()
        self.port = conf['network']['p2p_port']
        self.socket_udp = None
        self.socket_udp6 = None
        if not self.setup_udp_socket():
            self.logger.error("** Fail to setup UDP socket **")
            return
        self.listen_socket = None
        self.listen_socket6 = None
        self.max_connections = conf['network']['max_connections']
        if not self.setup_tcp_server():
            self.logger.error("** Fail to setup TCP server **")
            return

        if 'domains' not in conf:
            return
        for dm in conf['domains'].keys():
            domain_id = bbclib.convert_idstring_to_bytes(dm)
            if not self.use_global and domain_id == bbclib.domain_global_0:
                continue
            c = conf['domains'][dm]
            nw_module = c.get('module', 'simple_cluster')
            self.create_domain(domain_id=domain_id, network_module=nw_module)
            if 'special_domain' in c:
                c.pop('storage_type', None)
                c.pop('storage_path', None)
            else:
                self.core.ledger_manager.add_domain(domain_id)
                for asset_group_id_str, info in c['asset_group_ids'].items():
                    asset_group_id = bbclib.convert_idstring_to_bytes(asset_group_id_str)
                    self.core.asset_group_setup(domain_id, asset_group_id,
                                                c.get('storage_type', StorageType.FILESYSTEM),
                                                c.get('storage_path',None),
                                                c.get('advertise_in_domain0', False))
            for nd, info in c['static_nodes'].items():
                node_id, ipv4, ipv6, port = bbclib.convert_idstring_to_bytes(nd), info[0], info[1], info[2]
                self.add_static_node_to_domain(domain_id, node_id, ipv4, ipv6, port)
            for nd, info in c['peer_list'].items():
                node_id, ipv4, ipv6, port = bbclib.convert_idstring_to_bytes(nd), info[0], info[1], info[2]
                self.domains[domain_id].add_peer_node_ip46(node_id, ipv4, ipv6, port)

    def get_my_socket_info(self):
        """
        Return waiting port and my IP address

        :return:
        """
        ipv4 = self.ip_address
        if ipv4 is None or len(ipv4) == 0:
            ipv4 = "0.0.0.0"
        ipv6 = self.ip6_address
        if ipv6 is None or len(ipv6) == 0:
            ipv6 = "::"
        port = socket.htons(self.port).to_bytes(2, 'little')
        return socket.inet_pton(socket.AF_INET, ipv4), socket.inet_pton(socket.AF_INET6, ipv6), port

    def create_domain(self, domain_id=ZEROS, network_module=None, get_new_node_id=False):
        """
        Create domain and register user in the domain

        :param domain_id:
        :param network_module: string of module script file
        :param get_new_node_id: If True, the node_id is newly created again
        :return:
        """
        if domain_id in self.domains:
            return False

        nw_module = None
        if network_module is not None:
            if isinstance(network_module, bytes):
                network_module = network_module.decode()
            nw_module = __import__(network_module)

        if nw_module is None:
            return None

        conf = self.config.get_domain_config(domain_id, create_if_new=True)
        if 'node_id' not in conf or get_new_node_id:
            node_id = bbclib.get_random_id()
            conf['node_id'] = bbclib.convert_id_to_string(node_id)
            self.config.update_config()
        else:
            node_id = bbclib.convert_idstring_to_bytes(conf.get('node_id'))

        self.domains[domain_id] = nw_module.NetworkDomain(network=self, config=self.config,
                                                          domain_id=domain_id, node_id=node_id,
                                                          loglevel=self.logger.level, logname=self.logname)
        if domain_id != bbclib.domain_global_0:
            self.core.ledger_manager.add_domain(domain_id)
        return True

    def remove_domain(self, domain_id=ZEROS):
        """
        Remove domain (remove DHT)

        :param domain_id:
        :return:
        """
        if domain_id not in self.domains:
            return
        self.domains[domain_id].leave_domain()
        del self.domains[domain_id]
        if domain_id in self.asset_groups_to_advertise:
            self.asset_groups_to_advertise.remove(domain_id)
        if self.use_global:
            self.domains[bbclib.domain_global_0].advertise_asset_group_info()

    def add_static_node_to_domain(self, domain_id, node_id, ipv4, ipv6, port):
        """
        Add static peer node for the domain

        :param domain_id:
        :param node_id:
        :param ipv4:
        :param ipv6:
        :param port:
        :return:
        """
        if domain_id not in self.domains:
            return
        self.domains[domain_id].add_peer_node_ip46(node_id, ipv4, ipv6, port)
        conf = self.config.get_domain_config(domain_id)
        if node_id not in conf['static_nodes']:
            if not isinstance(ipv4, str):
                ipv4 = ipv4.decode()
            if not isinstance(ipv6, str):
                ipv6 = ipv6.decode()
            conf['static_nodes'][bbclib.convert_id_to_string(node_id)] = [ipv4, ipv6, port]

    def save_all_peer_lists(self):
        """
        Save all peer_lists in the config file

        :return:
        """
        self.logger.info("Saving the current peer lists")
        for domain_id in self.domains.keys():
            conf = self.config.get_domain_config(domain_id)
            conf['peer_list'] = dict()
            for node_id, nodeinfo in self.domains[domain_id].id_ip_mapping.items():
                nid = bbclib.convert_id_to_string(node_id)
                conf['peer_list'][nid] = [nodeinfo.ipv4, nodeinfo.ipv6, nodeinfo.port]
        self.logger.info("Done...")

    def send_raw_message(self, domain_id, ipv4, ipv6, port):
        """
        (internal use) Send raw message to the specified node

        :param domain_id:
        :param ipv4:
        :param ipv6:
        :param port:
        :return:
        """
        if domain_id not in self.domains:
            return False
        node_id = self.domains[domain_id].node_id
        nodeinfo = NodeInfo(ipv4=ipv4, ipv6=ipv6, port=port)
        query_entry = query_management.QueryEntry(expire_after=10,
                                                  callback_error=self.raw_ping,
                                                  data={KeyType.domain_id: domain_id,
                                                        KeyType.node_id: node_id,
                                                        KeyType.peer_info: nodeinfo},
                                                  retry_count=3)
        self.raw_ping(query_entry)
        return True

    def raw_ping(self, query_entry):
        msg = {
            KeyType.domain_id: query_entry.data[KeyType.domain_id],
            KeyType.node_id: query_entry.data[KeyType.node_id],
            KeyType.domain_ping: 0,
            KeyType.nonce: query_entry.nonce,
        }
        self.logger.debug("Send domain_ping to %s:%d" % (query_entry.data[KeyType.peer_info].ipv4,
                                                         query_entry.data[KeyType.peer_info].port))
        query_entry.update(fire_after=1)
        self.send_message_in_network(query_entry.data[KeyType.peer_info], PayloadType.Type_msgpack, msg)

    def receive_domain_ping(self, ip4, from_addr, msg):
        """
        Process received domain_ping. If KeyType.domain_ping value is 1, the sender of the ping is registered as static

        :param ip4:       True (from IPv4) / False (from IPv6)
        :param from_addr: sender address and port (None if TCP)
        :param msg:       the message body (already deserialized)
        :param payload_type: PayloadType value of msg
        :return:
        """
        if KeyType.domain_id not in msg or KeyType.node_id not in msg:
            return
        domain_id = msg[KeyType.domain_id]
        node_id = msg[KeyType.node_id]
        self.logger.debug("Receive domain_ping to domain %s" % (binascii.b2a_hex(domain_id[:4])))
        if domain_id not in self.domains:
            return
        if self.domains[domain_id].node_id == node_id:
            return

        if ip4:
            ipv4 = from_addr[0]
            ipv6 = "::"
        else:
            ipv4 = "0.0.0.0"
            ipv6 = from_addr[0]

        if msg[KeyType.domain_ping] == 1:
            query_entry = ticker.get_entry(msg[KeyType.nonce])
            query_entry.deactivate()
            self.add_static_node_to_domain(domain_id, node_id, ipv4, ipv6, from_addr[1])
            self.domains[domain_id].alive_check()
        else:
            msg = {
                KeyType.domain_id: domain_id,
                KeyType.node_id: self.domains[domain_id].node_id,
                KeyType.domain_ping: 1,
                KeyType.nonce: msg[KeyType.nonce],
            }
            nodeinfo = NodeInfo(ipv4=ipv4, ipv6=ipv6, port=from_addr[1])
            self.send_message_in_network(nodeinfo, PayloadType.Type_msgpack, msg)

    def get(self, query_entry):
        """
        (internal use) try to get resource data

        :param nonce:
        :param domain_id:
        :param resource_id:
        :param resource_type:
        :return:
        """
        domain_id = query_entry.data[KeyType.domain_id]
        if domain_id not in self.domains:
            return
        self.domains[domain_id].get_resource(query_entry)

    def put(self, domain_id=None, asset_group_id=None, resource_id=None,
            resource_type=ResourceType.Transaction_data, resource=None):
        """
        Put data in the DHT

        :param domain_id:
        :param asset_group_id:
        :param resource_id:
        :param resource_type:
        :param resource:
        :return:
        """
        if domain_id not in self.domains:
            return
        self.logger.debug("[%s] *** put(resource_id=%s) ****" % (self.domains[domain_id].shortname,
                                                                 binascii.b2a_hex(resource_id[:4])))
        self.domains[domain_id].put_resource(asset_group_id, resource_id, resource_type, resource)

    def route_message(self, domain_id=ZEROS, asset_group_id=None, dst_user_id=None, src_user_id=None,
                      msg_to_send=None, payload_type=PayloadType.Type_msgpack):
        """
        Find the destination host and send it

        :param domain_id:
        :param asset_group_id:
        :param src_user_id:   source user
        :param dst_user_id:   destination user
        :param msg_to_send:   content to send
        :param payload_type:  PayloadType value
        :return:
        """
        if domain_id not in self.domains:
            return False

        self.logger.debug("route_message to dst_user_id:%s" % (binascii.b2a_hex(dst_user_id[:2])))
        if self.domains[domain_id].is_registered_user(asset_group_id, dst_user_id):
            self.logger.debug(" -> directly to the app")
            self.core.send_message(msg_to_send)
            return True

        query_entry = query_management.QueryEntry(expire_after=DURATION_GIVEUP_PUT,
                                                  callback_expire=self.callback_route_failure,
                                                  callback=self.forward_message,
                                                  callback_error=self.domains[domain_id].send_p2p_message,
                                                  interval=INTERVAL_RETRY,
                                                  data={KeyType.domain_id: domain_id,
                                                        KeyType.asset_group_id: asset_group_id,
                                                        KeyType.source_node_id: src_user_id,
                                                        KeyType.resource_id: dst_user_id,
                                                        'payload_type': payload_type,
                                                        'msg_to_send': msg_to_send},
                                                  retry_count=ROUTE_RETRY_COUNT)
        self.domains[domain_id].send_p2p_message(query_entry)
        return True

    def forward_message(self, query_entry):
        """
        (internal use) forward message

        :param query_entry:
        :return:
        """
        if KeyType.peer_info in query_entry.data:
            nodeinfo = query_entry.data[KeyType.peer_info]
            domain_id = query_entry.data[KeyType.domain_id]
            payload_type = query_entry.data['payload_type']
            msg = self.domains[domain_id].make_message(dst_node_id=nodeinfo.node_id,
                                                       msg_type=InfraMessageTypeBase.MESSAGE_TO_USER)
            msg[KeyType.message] = query_entry.data['msg_to_send']
            self.logger.debug("[%s] forward_message to %s" % (binascii.b2a_hex(self.domains[domain_id].node_id[:2]),
                                                              binascii.b2a_hex(nodeinfo.node_id[:4])))
            self.send_message_in_network(nodeinfo, payload_type, msg=msg)
        else:
            self.logger.debug("[%s] forward_message to app" %
                              (binascii.b2a_hex(self.domains[query_entry.data[KeyType.domain_id]].node_id[:2])))
            self.core.send_message(query_entry.data['msg_to_send'])

    def callback_route_failure(self, query_entry):
        """
        (internal use) Called after several "route_message" trial

        :param query_entry:
        :return:
        """
        dat = query_entry.data['msg_to_send']
        print(dat)
        msg = bbc_core.make_message_structure(dat[KeyType.command], query_entry.data[KeyType.asset_group_id],
                                              query_entry.data[KeyType.source_node_id], dat[KeyType.query_id])
        self.core.error_reply(msg=msg, err_code=ENODESTINATION, txt="cannot find core node")

    def register_user_id(self, domain_id, asset_group_id, user_id):
        """
        Register user_id connecting directly to this node in the domain

        :param domain_id:
        :param asset_group_id:
        :param user_id:
        :return:
        """
        self.domains[domain_id].register_user_id(asset_group_id, user_id)

    def remove_user_id(self, asset_group_id, user_id):
        """
        Remove user_id from the domain

        :param asset_group_id:
        :param user_id:
        :return:
        """
        for domain_id in self.domains:
            self.domains[domain_id].unregister_user_id(asset_group_id, user_id)

    def disseminate_cross_ref(self, transaction_id, asset_group_id):
        """
        disseminate transaction_id in the network (domain_global_0)

        :param transaction_id:
        :param asset_group_id:
        :return:
        """
        if self.use_global:
            msg = self.domains[bbclib.domain_global_0].make_message(dst_node_id=None,
                                                                    msg_type=InfraMessageTypeBase.NOTIFY_CROSS_REF)
            data = bytearray()
            data.extend(to_2byte(1))
            data.extend(asset_group_id)
            data.extend(transaction_id)
            msg[KeyType.cross_refs] = bytes(data)
            self.domains[bbclib.domain_global_0].random_send(msg, NUM_CROSS_REF_COPY)
        else:
            self.core.add_cross_ref_into_list(asset_group_id, transaction_id)

    def send_message_in_network(self, nodeinfo, payload_type, msg):
        """
        Send message over a domain network

        :param nodeinfo: NodeInfo object
        :param payload_type: PayloadType value
        :param msg:  data body
        :return:
        """
        data_to_send = message_key_types.make_message(payload_type, msg)
        if len(data_to_send) > TCP_THRESHOLD_SIZE:
            send_data_by_tcp(ipv4=nodeinfo.ipv4, ipv6=nodeinfo.ipv6, port=nodeinfo.port, msg=data_to_send)
            return
        if nodeinfo.ipv4 != "":
            self.socket_udp.sendto(data_to_send, (nodeinfo.ipv4, nodeinfo.port))
            return
        if nodeinfo.ipv6 != "":
            self.socket_udp6.sendto(data_to_send, (nodeinfo.ipv6, nodeinfo.port))

    def setup_udp_socket(self):
        """
        (internal use) Setup UDP socket

        :return:
        """
        try:
            self.socket_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket_udp.bind(("0.0.0.0", self.port))
        except OSError:
            self.socket_udp = None
            self.logger.error("Socket error for IPv4")
        try:
            self.socket_udp6 = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            self.socket_udp6.bind(("::", self.port))
        except OSError:
            self.socket_udp6 = None
            self.logger.error("Socket error for IPv6")
        if self.socket_udp is None and self.socket_udp6 is None:
            return False
        th_nw_loop = threading.Thread(target=self.udp_message_loop)
        th_nw_loop.setDaemon(True)
        th_nw_loop.start()
        return True

    def udp_message_loop(self):
        """
        (internal use) message loop for UDP socket

        :return:
        """
        self.logger.debug("Start udp_message_loop")
        msg_parser = message_key_types.Message()
        # readfds = set([self.socket_udp, self.socket_udp6])
        readfds = set()
        if self.socket_udp:
            readfds.add(self.socket_udp)
        if self.socket_udp6:
            readfds.add(self.socket_udp6)
        try:
            while True:
                rready, wready, xready = select.select(readfds, [], [])
                for sock in rready:
                    data = None
                    ip4 = True
                    if sock is self.socket_udp:
                        data, addr = self.socket_udp.recvfrom(1500)
                    elif sock is self.socket_udp6:
                        data, addr = self.socket_udp6.recvfrom(1500)
                        ip4 = False
                    if data is not None:
                        msg_parser.recv(data)
                        msg = msg_parser.parse()
                        #self.logger.debug("Recv_UDP from %s: data=%s" % (addr, msg))
                        if msg_parser.payload_type == PayloadType.Type_msgpack:
                            if KeyType.domain_ping in msg:
                                self.receive_domain_ping(ip4, addr, msg)
                                continue
                            if KeyType.destination_node_id not in msg or KeyType.domain_id not in msg:
                                continue
                            if msg[KeyType.domain_id] in self.domains:
                                self.domains[msg[KeyType.domain_id]].process_message_base(ip4, addr, msg, msg_parser.payload_type)
        finally:
            for sock in readfds:
                sock.close()
            self.socket_udp = None
            self.socket_udp6 = None

    def setup_tcp_server(self):
        """
        (internal use) start tcp server

        :return:
        """
        try:
            self.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listen_socket.bind(("0.0.0.0", self.port))
            self.listen_socket.listen(self.max_connections)
            self.port = self.port
        except OSError:
            self.listen_socket = None
            self.logger.error("Socket error for IPv4")
        try:
            self.listen_socket6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            self.listen_socket6.bind(("::", self.port))
            self.listen_socket6.listen(self.max_connections)
            self.port = self.port
        except OSError:
            self.listen_socket6 = None
            self.logger.error("Socket error for IPv6")
        if self.listen_socket is None and self.listen_socket6 is None:
            return False
        th_tcp_loop = threading.Thread(target=self.tcpserver_loop)
        th_tcp_loop.setDaemon(True)
        th_tcp_loop.start()
        return True

    def tcpserver_loop(self):
        """
        (internal use) message loop for TCP socket

        :return:
        """
        self.logger.debug("Start tcpserver_loop")
        msg_parsers = dict()
        readfds = set()
        if self.listen_socket:
            readfds.add(self.listen_socket)
        if self.listen_socket6:
            readfds.add(self.listen_socket6)
        try:
            while True:
                rready, wready, xready = select.select(readfds, [], [])
                for sock in rready:
                    if sock is self.listen_socket:
                        conn, address = self.listen_socket.accept()
                        readfds.add(conn)
                        msg_parsers[conn] = message_key_types.Message()
                    elif sock is self.listen_socket6:
                        conn, address = self.listen_socket6.accept()
                        readfds.add(conn)
                        msg_parsers[conn] = message_key_types.Message()
                    else:
                        buf = sock.recv(8192)
                        if len(buf) == 0:
                            del msg_parsers[sock]
                            sock.close()
                            readfds.remove(sock)
                        else:
                            msg_parsers[sock].recv(buf)
                            while True:
                                msg = msg_parsers[sock].parse()
                                if msg is None:
                                    break
                                #self.logger.debug("Recv_TCP at %s: data=%s" % (sock.getsockname(), msg))
                                if msg_parsers[sock].payload_type == PayloadType.Type_msgpack:
                                    if KeyType.destination_node_id not in msg or KeyType.domain_id not in msg:
                                        continue
                                self.domains[msg[KeyType.domain_id]].process_message_base(True, None, msg,
                                                                                          msg_parsers[sock].payload_type)
        finally:
            for sock in readfds:
                sock.close()
            self.listen_socket = None
            self.listen_socket6 = None


class InfraMessageTypeBase:
    DOMAIN_PING = to_2byte(0)
    NOTIFY_LEAVE = to_2byte(1)
    NOTIFY_PEERLIST = to_2byte(2)
    START_TO_REFRESH = to_2byte(3)
    REQUEST_PING = to_2byte(4)
    RESPONSE_PING = to_2byte(5)

    NOTIFY_CROSS_REF = to_2byte(0, 0x10)        # only used in domain_global_0
    ADVERTISE_ASSET_GROUP = to_2byte(1, 0x10)   # only used in domain_global_0

    REQUEST_STORE = to_2byte(0, 0x40)
    RESPONSE_STORE = to_2byte(1, 0x40)
    RESPONSE_STORE_COPY = to_2byte(2, 0x40)
    REQUEST_FIND_USER = to_2byte(3, 0x40)
    RESPONSE_FIND_USER = to_2byte(4, 0x40)
    REQUEST_FIND_VALUE = to_2byte(5, 0x40)
    RESPONSE_FIND_VALUE = to_2byte(6, 0x40)
    MESSAGE_TO_USER = to_2byte(7, 0x40)


class DomainBase:
    """
    Base class of a domain
    """
    def __init__(self, network=None, config=None, domain_id=None, node_id=None, loglevel="all", logname=None):
        self.network = network
        self.config = config
        self.node_id = node_id
        self.domain_id = domain_id
        self.logger = logger.get_logger(key="domain:%s" % binascii.b2a_hex(domain_id[:4]).decode(),
                                        level=loglevel, logname=logname)
        if node_id is None:
            self.logger.error("node_id must be specified!")
            return
        self.shortname = binascii.b2a_hex(node_id[:2])  # for debugging
        self.default_payload_type = PayloadType.Type_msgpack
        self.id_ip_mapping = dict()
        self.registered_user_id = dict()
        self.user_id_forward_cache = dict()
        self.refresh_entry = None
        self.set_refresh_timer()

    def set_refresh_timer(self, interval=REFRESH_INTERVAL):
        """
        (internal use) set refresh timer

        :param interval:
        :return:
        """
        self.refresh_entry = query_management.exec_func_after(self.refresh_peer_list,
                                                              random.randint(int(interval / 2),
                                                               int(interval * 1.5))
                                                             )

    def refresh_peer_list(self, query_entry):
        """
        (internal use) refresh peer_list by alive_check

        :param query_entry:
        :return:
        """
        for nd in self.id_ip_mapping.keys():
            self.send_start_refresh(nd)
        self.alive_check()
        self.set_refresh_timer()

    def start_domain_manager(self):
        """
        (internal use) start domain manager loop

        :return:
        """
        th = threading.Thread(target=self.domain_manager_loop)
        th.setDaemon(True)
        th.start()

    def domain_manager_loop(self):
        """
        (internal use) maintain the domain (e.g., updating peer list and topology)

        :return:
        """
        pass

    def alive_check(self):
        """
        Check whether alive or not to update node list and to broadcast the list to others

        :return:
        """
        self.logger.error("Need to implement(override) alive_check()")

    def ping_response_check(self, query_entry):
        node_id = query_entry.data[KeyType.node_id]
        if node_id in self.id_ip_mapping and not self.id_ip_mapping[node_id].is_alive:
            del self.id_ip_mapping[node_id]

    def ping_with_retry(self, query_entry=None, node_id=None, retry_count=3):
        """
        Retry ping if response is not received within a given time

        :param query_entry:
        :param node_id:     target node_id (need for first trial)
        :param retry_count:
        :return:
        """
        if node_id is not None:
            query_entry = query_management.QueryEntry(expire_after=ALIVE_CHECK_PING_WAIT,
                                                      callback_error=self.ping_with_retry,
                                                      interval=1,
                                                      data={KeyType.node_id: node_id},
                                                      retry_count=retry_count)
        else:
            node_id = query_entry.data[KeyType.node_id]
        query_entry.update()
        self.send_ping(node_id, nonce=query_entry.nonce)

    def add_peer_node_ip46(self, node_id, ipv4, ipv6, port):
        """
        Add as a peer node (with ipv4 and ipv6 address)

        :param node_id:
        :param ipv4:
        :param ipv6:
        :param port:
        :return:
        """
        self.logger.debug("[%s] add_peer_node_ip46: nodeid=%s, port=%d" % (self.shortname,
                                                                           binascii.b2a_hex(node_id[:2]), port))
        self.id_ip_mapping[node_id] = NodeInfo(node_id=node_id, ipv4=ipv4, ipv6=ipv6, port=port)
        query_entry = query_management.QueryEntry(expire_after=ALIVE_CHECK_PING_WAIT,
                                                  callback_expire=self.ping_response_check,
                                                  data={KeyType.node_id: node_id},
                                                  retry_count=0)
        self.ping_with_retry(node_id=node_id, retry_count=3)

    def add_peer_node(self, node_id, ip4, addr_info):
        """
        Add as a peer node

        :param node_id:
        :param ip4: True (IPv4)/False (IPv6)
        :param addr_info: tuple of (address, port)
        :return:
        """
        if addr_info is None:
            return True
        port = addr_info[1]
        if node_id in self.id_ip_mapping:
            if ip4:
                self.logger.debug("[%s] add_peer_node: nodeid=%s, port=%d" % (self.shortname,
                                                                              binascii.b2a_hex(node_id[:2]),
                                                                              addr_info[1]))
                self.id_ip_mapping[node_id].update(ipv4=addr_info[0], port=port)
            else:
                self.id_ip_mapping[node_id].update(ipv6=addr_info[0], port=port)
            self.id_ip_mapping[node_id].touch()
            return False
        else:
            if ip4:
                self.logger.debug("[%s] add_peer_node: new! nodeid=%s, port=%d" % (self.shortname,
                                                                                   binascii.b2a_hex(node_id[:2]),
                                                                                   addr_info[1]))
                self.id_ip_mapping[node_id] = NodeInfo(node_id=node_id, ipv4=addr_info[0], ipv6=None, port=port)
            else:
                self.id_ip_mapping[node_id] = NodeInfo(node_id=node_id, ipv4=None, ipv6=addr_info[0], port=port)
            if self.refresh_entry.rest_of_time_to_expire() > 10:
                self.refresh_entry.update_expiration_time(5)
            return True

    def remove_peer_node(self, node_id=ZEROS):
        """
        Remove node_info from the id_ip_mapping

        :param id:
        :return:
        """
        self.id_ip_mapping.pop(node_id, None)

    def make_peer_list(self):
        """
        Make binary peer_list (the first entry of the returned result always include the info of the node itself)

        :return: binary data of count,[node_id,ipv4,ipv6,port],[node_id,ipv4,ipv6,port],[node_id,ipv4,ipv6,port],,,,
        """
        nodeinfo = bytearray()

        # the node itself
        nodeinfo.extend(self.node_id)
        for item in self.network.get_my_socket_info():
            nodeinfo.extend(item)
        count = 1

        # neighboring node
        for nd in self.id_ip_mapping.keys():
            count += 1
            for item in self.id_ip_mapping[nd].get_nodeinfo():
                nodeinfo.extend(item)

        nodes = bytearray(count.to_bytes(4, 'little'))
        nodes.extend(nodeinfo)
        return bytes(nodes)

    def print_peerlist(self):
        """
        Show peer list for debugging

        :return:
        """
        pass

    def get_neighbor_nodes(self):
        """
        Return neighbor nodes (for broadcasting message)

        :return:
        """
        pass

    def register_user_id(self, asset_group_id, user_id):
        """
        Register user_id that connect directly to this core node in the list

        :param asset_group_id:
        :param user_id:
        :return:
        """
        #self.logger.debug("[%s] register_user_id: %s" % (self.shortname,binascii.b2a_hex(user_id[:4])))
        self.registered_user_id.setdefault(asset_group_id, dict())
        self.registered_user_id[asset_group_id][user_id] = time.time()

    def unregister_user_id(self, asset_group_id, user_id):
        """
        (internal use) remove user_id from the list

        :param asset_group_id:
        :param user_id:
        :return:
        """
        if asset_group_id in self.registered_user_id:
            self.registered_user_id[asset_group_id].pop(user_id, None)
        if len(self.registered_user_id[asset_group_id]) == 0:
            self.registered_user_id.pop(asset_group_id, None)

    def is_registered_user(self, asset_group_id, user_id):
        """
        (internal use) check if the user_id is registered in the asset_group

        :param asset_group_id:
        :param user_id:
        :return:
        """
        #self.logger.debug("[%s] is_registered_user: %s" % (self.shortname, binascii.b2a_hex(user_id[:4])))
        try:
            if user_id in self.registered_user_id[asset_group_id]:
                return True
            return False
        except:
            return False

    def make_message(self, dst_node_id=None, nonce=None, msg_type=None):
        """
        (internal use) create message with basic components

        :param dst_node_id:
        :param nonce:
        :param msg_type:
        :return:
        """
        msg = {
            KeyType.source_node_id: self.node_id,
            KeyType.destination_node_id: dst_node_id,
            KeyType.domain_id: self.domain_id,
            KeyType.p2p_msg_type: msg_type,
        }
        if nonce is not None:
            msg[KeyType.nonce] = nonce
        return msg

    def send_message_to_peer(self, msg, payload_type=PayloadType.Type_msgpack):
        """
        Resolve socket for the target_id and call message send method in BBcNetwork

        :param msg:
        :param payload_type: PayloadType value
        :return:
        """
        target_id = msg[KeyType.destination_node_id]
        if target_id not in self.id_ip_mapping:
            self.logger.info("[%s] Fail to send message: no such node" % self.shortname)
            return False
        nodeinfo = self.id_ip_mapping[target_id]
        self.logger.debug("[%s] send_message_to_peer from %s to %s:type=%d:port=%d" %
                          (self.shortname,
                           binascii.b2a_hex(msg[KeyType.source_node_id][:2]),
                           binascii.b2a_hex(target_id[:2]),
                           int.from_bytes(msg[KeyType.p2p_msg_type],'big'), nodeinfo.port))

        self.network.send_message_in_network(nodeinfo, payload_type, msg=msg)
        return True

    def process_message_base(self, ip4, from_addr, msg, payload_type):
        """
        (internal use) process received message (common process for any kind of network module)

        :param ip4:       True (from IPv4) / False (from IPv6)
        :param from_addr: sender address and port (None if TCP)
        :param msg:       the message body (already deserialized)
        :param payload_type: PayloadType value of msg
        :return:
        """
        if KeyType.p2p_msg_type not in msg:
            return
        self.logger.debug("[%s] process_message(type=%d) from %s" %
                          (self.shortname,
                           int.from_bytes(msg[KeyType.p2p_msg_type], 'big'),
                           binascii.b2a_hex(msg[KeyType.source_node_id][:4])))
        if msg[KeyType.p2p_msg_type] == InfraMessageTypeBase.MESSAGE_TO_USER:
            if KeyType.message not in msg:
                return
            self.logger.debug("[%s] msg to app: %s" % (self.shortname, msg[KeyType.message]))
            self.network.core.send_message(msg[KeyType.message])

        elif msg[KeyType.p2p_msg_type] == InfraMessageTypeBase.REQUEST_PING:
            self.add_peer_node(msg[KeyType.source_node_id], ip4, from_addr)
            self.respond_ping(msg[KeyType.source_node_id], msg.get(KeyType.nonce))

        elif msg[KeyType.p2p_msg_type] == InfraMessageTypeBase.RESPONSE_PING:
            self.add_peer_node(msg[KeyType.source_node_id], ip4, from_addr)
            if KeyType.nonce in msg:
                query_entry = ticker.get_entry(msg[KeyType.nonce])
                if query_entry is not None:
                    query_entry.callback()

        elif msg[KeyType.p2p_msg_type] == InfraMessageTypeBase.RESPONSE_STORE:
            self.add_peer_node(msg[KeyType.source_node_id], ip4, from_addr)
            query_entry = ticker.get_entry(msg[KeyType.nonce])
            if query_entry is not None:
                query_entry.deactivate()

        elif msg[KeyType.p2p_msg_type] == InfraMessageTypeBase.NOTIFY_CROSS_REF:
            self.add_peer_node(msg[KeyType.source_node_id], ip4, from_addr)
            if KeyType.cross_refs in msg:
                dat = msg[KeyType.cross_refs]
                count = struct.unpack(">H", dat[:2])[0]
                ptr = 2
                for i in range(count):
                    asset_group_id = bytes(dat[ptr:ptr+32])
                    ptr += 32
                    transaction_id = bytes(dat[ptr:ptr+32])
                    ptr += 32
                    self.network.core.add_cross_ref_into_list(asset_group_id, transaction_id)

        elif msg[KeyType.p2p_msg_type] == InfraMessageTypeBase.NOTIFY_PEERLIST:
            self.renew_peerlist(msg[KeyType.peer_list])

        elif msg[KeyType.p2p_msg_type] == InfraMessageTypeBase.START_TO_REFRESH:
            self.add_peer_node(msg[KeyType.source_node_id], ip4, from_addr)
            self.refresh_entry.deactivate()
            self.set_refresh_timer()

        elif msg[KeyType.p2p_msg_type] == InfraMessageTypeBase.NOTIFY_LEAVE:
            self.remove_peer_node(msg[KeyType.source_node_id])

        else:
            self.process_message(ip4, from_addr, msg)

    def process_message(self, ip4, from_addr, msg):
        """
        (internal use) process received message for the network module (need to override)

        :param ip4:       True (from IPv4) / False (from IPv6)
        :param from_addr: sender address and port (None if TCP)
        :param msg:       the message body (already deserialized)
        :return:
        """
        pass

    def renew_peerlist(self, peerlist):
        """
        (internal use) send peer_list to renew those of others

        :param peerlist:
        :return:
        """
        need_update = True
        mapping = dict()
        count = int.from_bytes(peerlist[:4], 'little')
        for i in range(count-1):
            base = 4 + i*(32+4+16+2)
            node_id = peerlist[base:base+32]
            if node_id == self.node_id:
                need_update = False
                continue
            ipv4 = peerlist[base+32:base+36]
            ipv6 = peerlist[base+36:base+52]
            port = peerlist[base+52:base+54]
            mapping[node_id] = NodeInfo()
            mapping[node_id].recover_nodeinfo(node_id, ipv4, ipv6, port)
        self.id_ip_mapping = mapping
        if need_update:
            for nd in self.id_ip_mapping.keys():
                self.send_ping(nd, None)

    def send_ping(self, target_id, nonce=None):
        msg = self.make_message(dst_node_id=target_id, nonce=nonce, msg_type=InfraMessageTypeBase.REQUEST_PING)
        return self.send_message_to_peer(msg, self.default_payload_type)

    def respond_ping(self, target_id, nonce=None):
        msg = self.make_message(dst_node_id=target_id, nonce=nonce, msg_type=InfraMessageTypeBase.RESPONSE_PING)
        return self.send_message_to_peer(msg, self.default_payload_type)

    def send_store(self, target_id, nonce, asset_group_id, resource_id, resource, resource_type):
        op_type = InfraMessageTypeBase.REQUEST_STORE
        msg = self.make_message(dst_node_id=target_id, nonce=nonce, msg_type=op_type)
        msg[KeyType.asset_group_id] = asset_group_id
        msg[KeyType.resource_id] = resource_id
        msg[KeyType.resource] = resource
        msg[KeyType.resource_type] = resource_type
        return self.send_message_to_peer(msg, self.default_payload_type)

    def respond_store(self, target_id, nonce):
        msg = self.make_message(dst_node_id=target_id, nonce=nonce, msg_type=InfraMessageTypeBase.RESPONSE_STORE)
        return self.send_message_to_peer(msg, self.default_payload_type)

    def send_start_refresh(self, target_id):
        msg = self.make_message(dst_node_id=target_id, msg_type=InfraMessageTypeBase.START_TO_REFRESH)
        return self.send_message_to_peer(msg, self.default_payload_type)

    def leave_domain(self):
        msg = self.make_message(dst_node_id=ZEROS, nonce=None, msg_type=InfraMessageTypeBase.NOTIFY_LEAVE)
        nodelist = list(self.id_ip_mapping.keys())
        for nd in nodelist:
            msg[KeyType.destination_node_id] = nd
            self.send_message_to_peer(msg, self.default_payload_type)

    def random_send(self, msg, count):
        pass

    def get_resource(self, query_entry):
        pass

    def put_resource(self, asset_group_id, resource_id, resource_type, resource):
        pass

    def send_p2p_message(self, query_entry):
        pass


