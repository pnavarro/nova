# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2012 Pedro Navarro Perez
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
import uuid

from nova.virt import vif
from nova.openstack.common import cfg
from nova.virt.hyperv import networkutils

CONF = cfg.CONF
CONF.import_opt('vswitch_name', 'nova.virt.hyperv.vmops')


class HypervVswitchDriver(vif.VIFDriver):
    """VIF driver for Windows vswitch driver"""

    def __init__(self):
        self.utils = networkutils.NetworkUtils()

    def create_wvs_vif_port(self, iface_id):
        return self.utils.create_switch_port(CONF.vswitch_name, iface_id)

    def delete_wvs_vif_port(self, iface_id):
        self.utils.delete_switch_port(CONF.vswitch_name, iface_id)

    def plug(self, instance, vif):
        network, mapping = vif
        iface_id = mapping['vif_uuid']
        switch_port = self.create_wvs_vif_port(iface_id)
        vnic = self.utils.create_vnic()
        mac_address = vif['address'].replace(':', '')

        #Connect the new nic to the new port.
        vnic.Connection = [switch_port]
        vnic.ElementName = iface_id + ' nic'
        vnic.Address = mac_address
        vnic.StaticMacAddress = 'True'
        vnic.VirtualSystemIdentifiers = ['{' + str(uuid.uuid4()) + '}']
        return vnic

    def unplug(self, instance, vif):
        """Unplug the VIF by deleting the port from the bridge."""
        #Just delete the wvs port, the hyper-v driver cleans the VIF
        network, mapping = vif
        iface_id = mapping['vif_uuid']
        self.delete_wvs_vif_port(iface_id)
