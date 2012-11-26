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

import time

from eventlet.green import subprocess

from nova.openstack.common import cfg
from nova.openstack.common import log as logging
from nova.virt.hyperv import vmutils
from nova.virt.hyperv import basevolumeutils

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class VolumeUtils(basevolumeutils.BaseVolumeUtils):

        def __init__(self, conn_wmi):
            self._conn_wmi = conn_wmi

        def execute(self, *args, **kwargs):
            proc = subprocess.Popen(
                [args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stdout_value, stderr_value = proc.communicate()
            if stdout_value.find('The operation completed successfully') == -1:
                raise vmutils.HyperVException(_('An error has occurred when '
                    'calling the iscsi initiator: %s') % stdout_value)

        def login_storage_target(self, target_lun, target_iqn, target_portal):
            """Add target portal, list targets and logins to the target"""
            separator = target_portal.find(':')
            target_address = target_portal[:separator]
            target_port = target_portal[separator + 1:]
            #Adding target portal to iscsi initiator. Sending targets
            self.execute('iscsicli.exe ' + 'AddTargetPortal ' +
                target_address + ' ' + target_port +
                ' * * * * * * * * * * * * *')
            #Listing targets
            self.execute('iscsicli.exe ' + 'LisTargets')
            #Sending login
            self.execute('iscsicli.exe ' + 'qlogintarget ' + target_iqn)
            #Waiting the disk to be mounted. Research this
            time.sleep(CONF.hyperv_wait_between_attach_retry)

        def logout_storage_target(self, target_iqn):
            """ Logs out storage target through its session id """

            sessions = self._conn_wmi.query(
                    "SELECT * FROM MSiSCSIInitiator_SessionClass \
                    WHERE TargetName='" + target_iqn + "'")
            for session in sessions:
                self.execute_log_out(session.SessionId)

        def execute_log_out(self, session_id):
            """ Executes log out of the session described by its session ID """
            self.execute('iscsicli.exe ' + 'logouttarget ' + session_id)
