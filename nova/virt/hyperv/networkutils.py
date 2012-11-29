# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Cloudbase Solutions Srl / Pedro Navarro Perez
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

"""
Utility class for VM related operations.
"""

import sys
import uuid

from nova.openstack.common import cfg
from nova.openstack.common import log as logging

from nova.virt.hyperv.vmutils import HyperVException

# Check needed for unit testing on Unix
if sys.platform == 'win32':
    import wmi

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class NetworkUtils(object):

    def __init__(self):
        self.__conn = None

    @property
    def _conn(self):
        if self.__conn is None:
            self.__conn = wmi.WMI(moniker='//./root/virtualization')
        return self.__conn

    def get_switch_ports(self, vswitch_name):
        edge_ports = set()
        port_names = self._conn.Msvm_VirtualSwitch(
                ElementName=vswitch_name)[0]\
                .associators(wmi_result_class='Msvm_SwitchPort')
        for port in port_names:
            edge_ports.add(port.Name)
        return edge_ports

    def create_switch_port(self, vswitch_name, switch_port_name):
        """ Creates a switch port """
        switch_svc = self._conn.Msvm_VirtualSwitchManagementService()[0]
        vswitch_path = self._get_vswitch_path_by_name(self._conn, vswitch_name)
        (new_port, ret_val) = switch_svc.CreateSwitchPort(
            Name=switch_port_name,
            FriendlyName=switch_port_name,
            ScopeOfResidence="",
            VirtualSwitch=vswitch_path)
        if ret_val != 0:
            LOG.error(_('Failed creating a port on the vswitch'))
            raise HyperVException(_('Failed creating port for %s') % \
                                  vswitch_name)
        return new_port

    def delete_switch_port(self, vswitch_name, switch_port_name):
        """ Creates a switch port """
        switch_svc = self._conn.Msvm_VirtualSwitchManagementService()[0]
        vswitch_path = self._get_vswitch_path_by_name(self._conn, vswitch_name)
        ret_val = switch_svc.DeleteSwitchPort(SwitchPort=vswitch_path)
        if ret_val != 0:
            LOG.error(_('Failed deleting a port on the vswitch'))
            raise HyperVException(_('Failed deleting port for %s') % \
                                  vswitch_name)

    def disconnect_switch_port(self, vswitch_name, switch_port_name):
        """ Disconnects the switch port """
        switch_svc = self._conn.Msvm_VirtualSwitchManagementService()[0]
        switch_port_path = self._get_switch_port_path_by_name(
                        self._conn, switch_port_name)
        ret_val = switch_svc.DisconnectSwitchPort(SwitchPort=switch_port_path)
        if ret_val != 0:
            LOG.error(_('Failed disconnecting a port on the vswitch'))
            raise HyperVException(_('Failed disconnecting port for %s') % \
                                  vswitch_name)

    def _get_vswitch_path_by_name(self, vswitch_name):
        vswitch = self._conn.Msvm_VirtualSwitch(Name=vswitch_name)[0]
        return vswitch.path_()

    def _get_switch_port_path_by_name(self, switch_port_name):
        vswitch = self._conn.Msvm_SwitchPort(Name=switch_port_name)[0]
        return vswitch.path_()

    def create_vnic(self):
        syntheticnics_data = self._conn.Msvm_SyntheticEthernetPortSettingData()
        default_nic_data = [n for n in syntheticnics_data
                            if n.InstanceID.rfind('Default') > 0]
        new_nic_data = self._vmutils.clone_wmi_obj(self._conn,
                'Msvm_SyntheticEthernetPortSettingData',
                default_nic_data[0])
        return new_nic_data
