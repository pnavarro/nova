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

"""
Helper methods for operations related to the management of volumes,
and storage repositories
"""

import sys
import time

from nova import config
from nova.openstack.common import log as logging
from nova.virt.hyperv import volumeutils

LOG = logging.getLogger(__name__)
CONF = config.CONF


class VolumeUtilsV2(volumeutils.VolumeUtils):

        def _init_(self, conn_storage):
            super(VolumeUtilsV2, self).__init__()
            self._conn_storage = conn_storage

        def login_storage_target(self, target_lun, target_iqn,
            target_portal):
            """Add target portal, list targets and logins to the target"""
            separator = target_portal.find(':')
            target_address = target_portal[:separator]
            target_port = target_portal[separator + 1:]
            #Adding target portal to iscsi initiator. Sending targets
            portal = self._conn_storage.__getattr__("MSFT_iSCSITargetPortal")
            portal.New(TargetPortalAddress=target_address,
                       TargetPortalPortNumber=target_port)
            #Connecting to the target
            target = self._conn_storage.__getattr__("MSFT_iSCSITarget")
            target.Connect(NodeAddress=target_iqn,
                           IsPersistent=True)
            #Waiting the disk to be mounted. Research this
            time.sleep(CONF.hyperv_wait_between_attach_retry)

        def logout_storage_target(self, target_iqn):
            """ Logs out storage target through its session id """

            target = self._conn_storage.MSFT_iSCSITarget(
                    NodeAddress=target_iqn)[0]
            if target.IsConnected:
                session = self._conn_storage.MSFT_iSCSISession(
                        TargetNodeAddress=target_iqn)[0]
                if session.IsPersistent:
                    session.Unregister()
                target.Disconnect()

        def execute_log_out(self, session_id):
            session = self._conn_wmi.MSiSCSIInitiator_SessionClass(
                    SessionId=session_id)[0]
            self.logout_storage_target(session.TargetName)
