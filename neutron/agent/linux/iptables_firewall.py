# Copyright 2012, Nachi Ueno, NTT MCL, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import netaddr
from oslo.config import cfg

from neutron.agent import firewall
from neutron.agent.linux import ipset_manager
from neutron.agent.linux import iptables_comments as ic
from neutron.agent.linux import iptables_manager
from neutron.common import constants
from neutron.common import ipv6_utils
from neutron.i18n import _LI
from neutron.openstack.common import log as logging


LOG = logging.getLogger(__name__)
SG_CHAIN = 'sg-chain'
INGRESS_DIRECTION = 'ingress'
EGRESS_DIRECTION = 'egress'
SPOOF_FILTER = 'spoof-filter'
CHAIN_NAME_PREFIX = {INGRESS_DIRECTION: 'i',
                     EGRESS_DIRECTION: 'o',
                     SPOOF_FILTER: 's'}
DIRECTION_IP_PREFIX = {'ingress': 'source_ip_prefix',
                       'egress': 'dest_ip_prefix'}
IPSET_DIRECTION = {INGRESS_DIRECTION: 'src',
                   EGRESS_DIRECTION: 'dst'}
LINUX_DEV_LEN = 14
comment_rule = iptables_manager.comment_rule


class IptablesFirewallDriver(firewall.FirewallDriver):
    """Driver which enforces security groups through iptables rules."""
    IPTABLES_DIRECTION = {INGRESS_DIRECTION: 'physdev-out',
                          EGRESS_DIRECTION: 'physdev-in'}

    def __init__(self):
        self.root_helper = cfg.CONF.AGENT.root_helper
        self.iptables = iptables_manager.IptablesManager(
            root_helper=self.root_helper,
            use_ipv6=ipv6_utils.is_enabled())
        # TODO(majopela, shihanzhang): refactor out ipset to a separate
        # driver composed over this one
        self.ipset = ipset_manager.IpsetManager(root_helper=self.root_helper)
        # list of port which has security group
        self.filtered_ports = {}
        self._add_fallback_chain_v4v6()
        self._defer_apply = False
        self._pre_defer_filtered_ports = None
        # List of security group rules for ports residing on this host
        self.sg_rules = {}
        self.pre_sg_rules = None
        # List of security group member ips for ports residing on this host
        self.sg_members = {}
        self.pre_sg_members = None
        self.enable_ipset = cfg.CONF.SECURITYGROUP.enable_ipset

    @property
    def ports(self):
        return self.filtered_ports

    def update_security_group_rules(self, sg_id, sg_rules):
        LOG.debug("Update rules of security group (%s)", sg_id)
        self.sg_rules[sg_id] = sg_rules

    def update_security_group_members(self, sg_id, sg_members):
        LOG.debug("Update members of security group (%s)", sg_id)
        self.sg_members[sg_id] = sg_members

    def prepare_port_filter(self, port):
        LOG.debug("Preparing device (%s) filter", port['device'])
        self._remove_chains()
        self.filtered_ports[port['device']] = port
        # each security group has it own chains
        self._setup_chains()
        self.iptables.apply()

    def update_port_filter(self, port):
        LOG.debug("Updating device (%s) filter", port['device'])
        if port['device'] not in self.filtered_ports:
            LOG.info(_LI('Attempted to update port filter which is not '
                         'filtered %s'), port['device'])
            return
        self._remove_chains()
        self.filtered_ports[port['device']] = port
        self._setup_chains()
        self.iptables.apply()

    def remove_port_filter(self, port):
        LOG.debug("Removing device (%s) filter", port['device'])
        if not self.filtered_ports.get(port['device']):
            LOG.info(_LI('Attempted to remove port filter which is not '
                         'filtered %r'), port)
            return
        self._remove_chains()
        self.filtered_ports.pop(port['device'], None)
        self._setup_chains()
        self.iptables.apply()

    def _setup_chains(self):
        """Setup ingress and egress chain for a port."""
        if not self._defer_apply:
            self._setup_chains_apply(self.filtered_ports)

    def _setup_chains_apply(self, ports):
        self._add_chain_by_name_v4v6(SG_CHAIN)
        for port in ports.values():
            self._setup_chain(port, INGRESS_DIRECTION)
            self._setup_chain(port, EGRESS_DIRECTION)
            self.iptables.ipv4['filter'].add_rule(SG_CHAIN, '-j ACCEPT')
            self.iptables.ipv6['filter'].add_rule(SG_CHAIN, '-j ACCEPT')

    def _remove_chains(self):
        """Remove ingress and egress chain for a port."""
        if not self._defer_apply:
            self._remove_chains_apply(self.filtered_ports)

    def _remove_chains_apply(self, ports):
        for port in ports.values():
            self._remove_chain(port, INGRESS_DIRECTION)
            self._remove_chain(port, EGRESS_DIRECTION)
            self._remove_chain(port, SPOOF_FILTER)
        self._remove_chain_by_name_v4v6(SG_CHAIN)

    def _setup_chain(self, port, DIRECTION):
        self._add_chain(port, DIRECTION)
        self._add_rules_by_security_group(port, DIRECTION)

    def _remove_chain(self, port, DIRECTION):
        chain_name = self._port_chain_name(port, DIRECTION)
        self._remove_chain_by_name_v4v6(chain_name)

    def _add_fallback_chain_v4v6(self):
        self.iptables.ipv4['filter'].add_chain('sg-fallback')
        self.iptables.ipv4['filter'].add_rule('sg-fallback', '-j DROP',
                                              comment=ic.UNMATCH_DROP)
        self.iptables.ipv6['filter'].add_chain('sg-fallback')
        self.iptables.ipv6['filter'].add_rule('sg-fallback', '-j DROP',
                                              comment=ic.UNMATCH_DROP)

    def _add_chain_by_name_v4v6(self, chain_name):
        self.iptables.ipv6['filter'].add_chain(chain_name)
        self.iptables.ipv4['filter'].add_chain(chain_name)

    def _remove_chain_by_name_v4v6(self, chain_name):
        self.iptables.ipv4['filter'].remove_chain(chain_name)
        self.iptables.ipv6['filter'].remove_chain(chain_name)

    def _add_rules_to_chain_v4v6(self, chain_name, ipv4_rules, ipv6_rules,
                                 comment=None):
        for rule in ipv4_rules:
            self.iptables.ipv4['filter'].add_rule(chain_name, rule,
                                                  comment=comment)

        for rule in ipv6_rules:
            self.iptables.ipv6['filter'].add_rule(chain_name, rule,
                                                  comment=comment)

    def _get_device_name(self, port):
        return port['device']

    def _add_chain(self, port, direction):
        chain_name = self._port_chain_name(port, direction)
        self._add_chain_by_name_v4v6(chain_name)

        # Note(nati) jump to the security group chain (SG_CHAIN)
        # This is needed because the packet may much two rule in port
        # if the two port is in the same host
        # We accept the packet at the end of SG_CHAIN.

        # jump to the security group chain
        device = self._get_device_name(port)
        jump_rule = ['-m physdev --%s %s --physdev-is-bridged '
                     '-j $%s' % (self.IPTABLES_DIRECTION[direction],
                                 device,
                                 SG_CHAIN)]
        self._add_rules_to_chain_v4v6('FORWARD', jump_rule, jump_rule,
                                      comment=ic.VM_INT_SG)

        # jump to the chain based on the device
        jump_rule = ['-m physdev --%s %s --physdev-is-bridged '
                     '-j $%s' % (self.IPTABLES_DIRECTION[direction],
                                 device,
                                 chain_name)]
        self._add_rules_to_chain_v4v6(SG_CHAIN, jump_rule, jump_rule,
                                      comment=ic.SG_TO_VM_SG)

        if direction == EGRESS_DIRECTION:
            self._add_rules_to_chain_v4v6('INPUT', jump_rule, jump_rule,
                                          comment=ic.INPUT_TO_SG)

    def _split_sgr_by_ethertype(self, security_group_rules):
        ipv4_sg_rules = []
        ipv6_sg_rules = []
        for rule in security_group_rules:
            if rule.get('ethertype') == constants.IPv4:
                ipv4_sg_rules.append(rule)
            elif rule.get('ethertype') == constants.IPv6:
                if rule.get('protocol') == 'icmp':
                    rule['protocol'] = 'icmpv6'
                ipv6_sg_rules.append(rule)
        return ipv4_sg_rules, ipv6_sg_rules

    def _select_sgr_by_direction(self, port, direction):
        return [rule
                for rule in port.get('security_group_rules', [])
                if rule['direction'] == direction]

    def _setup_spoof_filter_chain(self, port, table, mac_ip_pairs, rules):
        if mac_ip_pairs:
            chain_name = self._port_chain_name(port, SPOOF_FILTER)
            table.add_chain(chain_name)
            for mac, ip in mac_ip_pairs:
                if ip is None:
                    # If fixed_ips is [] this rule will be added to the end
                    # of the list after the allowed_address_pair rules.
                    table.add_rule(chain_name,
                                   '-m mac --mac-source %s -j RETURN'
                                   % mac, comment=ic.PAIR_ALLOW)
                else:
                    table.add_rule(chain_name,
                                   '-m mac --mac-source %s -s %s -j RETURN'
                                   % (mac, ip), comment=ic.PAIR_ALLOW)
            table.add_rule(chain_name, '-j DROP', comment=ic.PAIR_DROP)
            rules.append('-j $%s' % chain_name)

    def _build_ipv4v6_mac_ip_list(self, mac, ip_address, mac_ipv4_pairs,
                                  mac_ipv6_pairs):
        if netaddr.IPNetwork(ip_address).version == 4:
            mac_ipv4_pairs.append((mac, ip_address))
        else:
            mac_ipv6_pairs.append((mac, ip_address))

    def _spoofing_rule(self, port, ipv4_rules, ipv6_rules):
        #Note(nati) allow dhcp or RA packet
        ipv4_rules += [comment_rule('-p udp -m udp --sport 68 --dport 67 '
                                    '-j RETURN', comment=ic.DHCP_CLIENT)]
        ipv6_rules += [comment_rule('-p icmpv6 -j RETURN',
                                    comment=ic.IPV6_RA_ALLOW)]
        ipv6_rules += [comment_rule('-p udp -m udp --sport 546 --dport 547 '
                                    '-j RETURN', comment=None)]
        mac_ipv4_pairs = []
        mac_ipv6_pairs = []

        if isinstance(port.get('allowed_address_pairs'), list):
            for address_pair in port['allowed_address_pairs']:
                self._build_ipv4v6_mac_ip_list(address_pair['mac_address'],
                                               address_pair['ip_address'],
                                               mac_ipv4_pairs,
                                               mac_ipv6_pairs)

        for ip in port['fixed_ips']:
            self._build_ipv4v6_mac_ip_list(port['mac_address'], ip,
                                           mac_ipv4_pairs, mac_ipv6_pairs)
        if not port['fixed_ips']:
            mac_ipv4_pairs.append((port['mac_address'], None))
            mac_ipv6_pairs.append((port['mac_address'], None))

        self._setup_spoof_filter_chain(port, self.iptables.ipv4['filter'],
                                       mac_ipv4_pairs, ipv4_rules)
        self._setup_spoof_filter_chain(port, self.iptables.ipv6['filter'],
                                       mac_ipv6_pairs, ipv6_rules)

    def _drop_dhcp_rule(self, ipv4_rules, ipv6_rules):
        #Note(nati) Drop dhcp packet from VM
        ipv4_rules += [comment_rule('-p udp -m udp --sport 67 --dport 68 '
                                    '-j DROP', comment=ic.DHCP_SPOOF)]
        ipv6_rules += [comment_rule('-p udp -m udp --sport 547 --dport 546 '
                                    '-j DROP', comment=None)]

    def _accept_inbound_icmpv6(self):
        # Allow multicast listener, neighbor solicitation and
        # neighbor advertisement into the instance
        icmpv6_rules = []
        for icmp6_type in constants.ICMPV6_ALLOWED_TYPES:
            icmpv6_rules += ['-p icmpv6 --icmpv6-type %s -j RETURN' %
                             icmp6_type]
        return icmpv6_rules

    def _select_sg_rules_for_port(self, port, direction):
        sg_ids = port.get('security_groups', [])
        port_rules = []
        fixed_ips = port.get('fixed_ips', [])
        for sg_id in sg_ids:
            for rule in self.sg_rules.get(sg_id, []):
                if rule['direction'] == direction:
                    if self.enable_ipset:
                        port_rules.append(rule)
                        continue
                    remote_group_id = rule.get('remote_group_id')
                    if not remote_group_id:
                        port_rules.append(rule)
                        continue
                    ethertype = rule['ethertype']
                    for ip in self.sg_members[remote_group_id][ethertype]:
                        if ip in fixed_ips:
                            continue
                        ip_rule = rule.copy()
                        direction_ip_prefix = DIRECTION_IP_PREFIX[direction]
                        ip_rule[direction_ip_prefix] = str(
                            netaddr.IPNetwork(ip).cidr)
                        port_rules.append(ip_rule)
        return port_rules

    def _get_remote_sg_ids(self, port, direction):
        sg_ids = port.get('security_groups', [])
        remote_sg_ids = {constants.IPv4: [], constants.IPv6: []}
        for sg_id in sg_ids:
            for rule in self.sg_rules.get(sg_id, []):
                if rule['direction'] == direction:
                    remote_sg_id = rule.get('remote_group_id')
                    ether_type = rule.get('ethertype')
                    if remote_sg_id and ether_type:
                        remote_sg_ids[ether_type].append(remote_sg_id)
        return remote_sg_ids

    def _add_rules_by_security_group(self, port, direction):
        # select rules for current port and direction
        security_group_rules = self._select_sgr_by_direction(port, direction)
        security_group_rules += self._select_sg_rules_for_port(port, direction)
        # make sure ipset members are updated for remote security groups
        if self.enable_ipset:
            remote_sg_ids = self._get_remote_sg_ids(port, direction)
            self._update_ipset_members(remote_sg_ids)
        # split groups by ip version
        # for ipv4, iptables command is used
        # for ipv6, iptables6 command is used
        ipv4_sg_rules, ipv6_sg_rules = self._split_sgr_by_ethertype(
            security_group_rules)
        ipv4_iptables_rules = []
        ipv6_iptables_rules = []
        # include fixed egress/ingress rules
        if direction == EGRESS_DIRECTION:
            self._add_fixed_egress_rules(port,
                                         ipv4_iptables_rules,
                                         ipv6_iptables_rules)
        elif direction == INGRESS_DIRECTION:
            ipv6_iptables_rules += self._accept_inbound_icmpv6()
        # include IPv4 and IPv6 iptable rules from security group
        ipv4_iptables_rules += self._convert_sgr_to_iptables_rules(
            ipv4_sg_rules)
        ipv6_iptables_rules += self._convert_sgr_to_iptables_rules(
            ipv6_sg_rules)
        # finally add the rules to the port chain for a given direction
        self._add_rules_to_chain_v4v6(self._port_chain_name(port, direction),
                                      ipv4_iptables_rules,
                                      ipv6_iptables_rules)

    def _add_fixed_egress_rules(self, port, ipv4_iptables_rules,
                                ipv6_iptables_rules):
        self._spoofing_rule(port,
                            ipv4_iptables_rules,
                            ipv6_iptables_rules)
        self._drop_dhcp_rule(ipv4_iptables_rules, ipv6_iptables_rules)

    def _get_current_sg_member_ips(self, sg_id, ethertype):
        return self.sg_members.get(sg_id, {}).get(ethertype, [])

    def _update_ipset_members(self, security_group_ids):
        for ethertype, sg_ids in security_group_ids.items():
            for sg_id in sg_ids:
                current_ips = self._get_current_sg_member_ips(sg_id, ethertype)
                if current_ips:
                    self.ipset.set_members(sg_id, ethertype, current_ips)

    def _generate_ipset_chain(self, sg_rule, remote_gid):
        iptables_rules = []
        args = self._protocol_arg(sg_rule.get('protocol'))
        args += self._port_arg('sport',
                               sg_rule.get('protocol'),
                               sg_rule.get('source_port_range_min'),
                               sg_rule.get('source_port_range_max'))
        args += self._port_arg('dport',
                               sg_rule.get('protocol'),
                               sg_rule.get('port_range_min'),
                               sg_rule.get('port_range_max'))
        direction = sg_rule.get('direction')
        ethertype = sg_rule.get('ethertype')

        if self.ipset.set_exists(remote_gid, ethertype):
            args += ['-m set', '--match-set',
                     self.ipset.get_name(remote_gid, ethertype),
                     IPSET_DIRECTION[direction]]
            args += ['-j RETURN']
            iptables_rules += [' '.join(args)]
        return iptables_rules

    def _convert_sgr_to_iptables_rules(self, security_group_rules):
        iptables_rules = []
        self._drop_invalid_packets(iptables_rules)
        self._allow_established(iptables_rules)
        for rule in security_group_rules:
            if self.enable_ipset:
                remote_gid = rule.get('remote_group_id')
                if remote_gid:
                    iptables_rules.extend(
                        self._generate_ipset_chain(rule, remote_gid))
                    continue
            # These arguments MUST be in the format iptables-save will
            # display them: source/dest, protocol, sport, dport, target
            # Otherwise the iptables_manager code won't be able to find
            # them to preserve their [packet:byte] counts.
            args = self._ip_prefix_arg('s',
                                       rule.get('source_ip_prefix'))
            args += self._ip_prefix_arg('d',
                                        rule.get('dest_ip_prefix'))
            args += self._protocol_arg(rule.get('protocol'))
            args += self._port_arg('sport',
                                   rule.get('protocol'),
                                   rule.get('source_port_range_min'),
                                   rule.get('source_port_range_max'))
            args += self._port_arg('dport',
                                   rule.get('protocol'),
                                   rule.get('port_range_min'),
                                   rule.get('port_range_max'))
            args += ['-j RETURN']
            iptables_rules += [' '.join(args)]

        iptables_rules += [comment_rule('-j $sg-fallback',
                                        comment=ic.UNMATCHED)]

        return iptables_rules

    def _drop_invalid_packets(self, iptables_rules):
        # Always drop invalid packets
        iptables_rules += [comment_rule('-m state --state ' 'INVALID -j DROP',
                                        comment=ic.INVALID_DROP)]
        return iptables_rules

    def _allow_established(self, iptables_rules):
        # Allow established connections
        iptables_rules += [comment_rule(
            '-m state --state RELATED,ESTABLISHED -j RETURN',
            comment=ic.ALLOW_ASSOC)]
        return iptables_rules

    def _protocol_arg(self, protocol):
        if not protocol:
            return []

        iptables_rule = ['-p', protocol]
        # iptables always adds '-m protocol' for udp and tcp
        if protocol in ['udp', 'tcp']:
            iptables_rule += ['-m', protocol]
        return iptables_rule

    def _port_arg(self, direction, protocol, port_range_min, port_range_max):
        if (protocol not in ['udp', 'tcp', 'icmp', 'icmpv6']
            or not port_range_min):
            return []

        if protocol in ['icmp', 'icmpv6']:
            # Note(xuhanp): port_range_min/port_range_max represent
            # icmp type/code when protocol is icmp or icmpv6
            # icmp code can be 0 so we cannot use "if port_range_max" here
            if port_range_max is not None:
                return ['--%s-type' % protocol,
                        '%s/%s' % (port_range_min, port_range_max)]
            return ['--%s-type' % protocol, '%s' % port_range_min]
        elif port_range_min == port_range_max:
            return ['--%s' % direction, '%s' % (port_range_min,)]
        else:
            return ['-m', 'multiport',
                    '--%ss' % direction,
                    '%s:%s' % (port_range_min, port_range_max)]

    def _ip_prefix_arg(self, direction, ip_prefix):
        #NOTE (nati) : source_group_id is converted to list of source_
        # ip_prefix in server side
        if ip_prefix:
            return ['-%s' % direction, ip_prefix]
        return []

    def _port_chain_name(self, port, direction):
        return iptables_manager.get_chain_name(
            '%s%s' % (CHAIN_NAME_PREFIX[direction], port['device'][3:]))

    def filter_defer_apply_on(self):
        if not self._defer_apply:
            self.iptables.defer_apply_on()
            self._pre_defer_filtered_ports = dict(self.filtered_ports)
            self.pre_sg_members = dict(self.sg_members)
            self.pre_sg_rules = dict(self.sg_rules)
            self._defer_apply = True

    def _remove_unused_security_group_info(self):
        need_removed_ipsets = {constants.IPv4: set(),
                               constants.IPv6: set()}
        need_removed_security_groups = set()
        remote_group_ids = {constants.IPv4: set(),
                            constants.IPv6: set()}
        current_group_ids = set()
        for port in self.filtered_ports.values():
            for direction in INGRESS_DIRECTION, EGRESS_DIRECTION:
                for ethertype, sg_ids in self._get_remote_sg_ids(
                        port, direction).items():
                    remote_group_ids[ethertype].update(sg_ids)
            groups = port.get('security_groups', [])
            current_group_ids.update(groups)

        for ethertype in [constants.IPv4, constants.IPv6]:
            need_removed_ipsets[ethertype].update(
                [x for x in self.pre_sg_members if x not in remote_group_ids[
                    ethertype]])
            need_removed_security_groups.update(
                [x for x in self.pre_sg_rules if x not in current_group_ids])

        # Remove unused ip sets (sg_members and kernel ipset if we
        # are using ipset)
        for ethertype, remove_set_ids in need_removed_ipsets.items():
            for remove_set_id in remove_set_ids:
                if self.sg_members.get(remove_set_id, {}).get(ethertype, []):
                    self.sg_members[remove_set_id][ethertype] = []
                if self.enable_ipset:
                    self.ipset.destroy(remove_set_id, ethertype)

        # Remove unused remote security group member ips
        sg_ids = self.sg_members.keys()
        for sg_id in sg_ids:
            if not (self.sg_members[sg_id].get(constants.IPv4, [])
                    or self.sg_members[sg_id].get(constants.IPv6, [])):
                self.sg_members.pop(sg_id, None)

        # Remove unused security group rules
        for remove_group_id in need_removed_security_groups:
            if remove_group_id in self.sg_rules:
                self.sg_rules.pop(remove_group_id, None)

    def filter_defer_apply_off(self):
        if self._defer_apply:
            self._defer_apply = False
            self._remove_chains_apply(self._pre_defer_filtered_ports)
            self._setup_chains_apply(self.filtered_ports)
            self.iptables.defer_apply_off()
            self._remove_unused_security_group_info()
            self._pre_defer_filtered_ports = None


class OVSHybridIptablesFirewallDriver(IptablesFirewallDriver):
    OVS_HYBRID_TAP_PREFIX = constants.TAP_DEVICE_PREFIX

    def _port_chain_name(self, port, direction):
        return iptables_manager.get_chain_name(
            '%s%s' % (CHAIN_NAME_PREFIX[direction], port['device']))

    def _get_device_name(self, port):
        return (self.OVS_HYBRID_TAP_PREFIX + port['device'])[:LINUX_DEV_LEN]
