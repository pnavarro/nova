# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Cloudbase Solutions Srl
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
Management class for basic VM operations.
"""
import os
import uuid

from nova.api.metadata import base as instance_metadata
from nova import exception
from nova.openstack.common import cfg
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova import utils
from nova.virt import configdrive
from nova.virt.hyperv import baseops
from nova.virt.hyperv import constants
from nova.virt.hyperv import vmutils

LOG = logging.getLogger(__name__)

hyperv_opts = [
    cfg.StrOpt('vswitch_name',
                default=None,
                help='Default vSwitch Name, '
                    'if none provided first external is used'),
    cfg.BoolOpt('limit_cpu_features',
               default=False,
               help='Required for live migration among '
                    'hosts with different CPU features'),
    cfg.BoolOpt('config_drive_inject_password',
               default=False,
               help='Sets the admin password in the config drive image'),
    cfg.StrOpt('qemu_img_cmd',
               default="qemu-img.exe",
               help='qemu-img is used to convert between '
                    'different image types'),
    cfg.BoolOpt('config_drive_cdrom',
               default=False,
               help='Attaches the Config Drive image as a cdrom drive '
                    'instead of a disk drive')
    ]

CONF = cfg.CONF
CONF.register_opts(hyperv_opts)
CONF.import_opt('use_cow_images', 'nova.config')


class VMOps(baseops.BaseOps):
    def __init__(self, volumeops):
        super(VMOps, self).__init__()

        self._vmutils = vmutils.VMUtils()
        self._volumeops = volumeops

    def list_instances(self):
        """ Return the names of all the instances known to Hyper-V. """
        vms = [v.ElementName
                for v in self._conn.Msvm_ComputerSystem(['ElementName'],
                    Caption="Virtual Machine")]
        return vms

    def get_info(self, instance):
        """Get information about the VM"""
        LOG.debug(_("get_info called for instance"), instance=instance)
        instance_name = instance["name"]
        return self._get_info(instance_name)

    def _get_info(self, instance_name):
        vm = self._vmutils.lookup(self._conn, instance_name)
        if vm is None:
            raise exception.InstanceNotFound(instance=instance_name)
        vm = self._conn.Msvm_ComputerSystem(
            ElementName=instance_name)[0]
        vs_man_svc = self._conn.Msvm_VirtualSystemManagementService()[0]
        vmsettings = vm.associators(
                       wmi_association_class='Msvm_SettingsDefineState',
                       wmi_result_class='Msvm_VirtualSystemSettingData')
        settings_paths = [v.path_() for v in vmsettings]
        #See http://msdn.microsoft.com/en-us/library/cc160706%28VS.85%29.aspx
        summary_info = vs_man_svc.GetSummaryInformation(
                                       [constants.VM_SUMMARY_NUM_PROCS,
                                        constants.VM_SUMMARY_ENABLED_STATE,
                                        constants.VM_SUMMARY_MEMORY_USAGE,
                                        constants.VM_SUMMARY_UPTIME],
                                            settings_paths)[1]
        info = summary_info[0]

        LOG.debug(_("hyperv vm state: %s"), info.EnabledState)
        state = constants.HYPERV_POWER_STATE[info.EnabledState]
        memusage = str(info.MemoryUsage)
        numprocs = str(info.NumberOfProcessors)
        uptime = str(info.UpTime)

        LOG.debug(_("Got Info for vm %(instance_name)s: state=%(state)d,"
                " mem=%(memusage)s, num_cpu=%(numprocs)s,"
                " uptime=%(uptime)s"), locals())

        return {'state': state,
                'max_mem': info.MemoryUsage,
                'mem': info.MemoryUsage,
                'num_cpu': info.NumberOfProcessors,
                'cpu_time': info.UpTime}

    def spawn(self, context, instance, image_meta, injected_files,
        admin_password, network_info, block_device_info=None):
        """ Create a new VM and start it."""
        instance_name = instance["name"]
        vm = self._vmutils.lookup(self._conn, instance_name)
        if vm is not None:
            raise exception.InstanceExists(name=instance_name)

        ebs_root = self._volumeops.volume_in_mapping(
            self._volumeops.get_default_root_device(),
           block_device_info)

        #If is not a boot from volume spawn
        if not (ebs_root):
            #Fetch the file, assume it is a VHD file.
            vhdfile = self._vmutils.get_vhd_path(instance_name)
            try:
                self._cache_image(fn=self._vmutils.fetch_image,
                  context=context,
                  target=vhdfile,
                  fname=instance['image_ref'],
                  image_id=instance['image_ref'],
                  user=instance['user_id'],
                  project=instance['project_id'],
                  cow=CONF.use_cow_images)
            except Exception as exn:
                LOG.exception(_('cache image failed: %s'), exn)
                self.destroy(instance)

        try:
            self._create_vm(instance)

            if not ebs_root:
                self._attach_ide_drive(instance['name'], vhdfile, 0, 0,
                    constants.IDE_DISK)
            else:
                self._volumeops.attach_boot_volume(block_device_info,
                                             instance_name)

            #A SCSI controller for volumes connection is created
            self._create_scsi_controller(instance['name'])

            for vif in network_info:
                mac_address = vif['address'].replace(':', '')
                self._create_nic(instance['name'], mac_address)

            if configdrive.required_by(instance):
                self._create_config_drive(instance, injected_files,
                    admin_password)

            LOG.debug(_('Starting VM %s '), instance_name)
            self._set_vm_state(instance['name'], 'Enabled')
            LOG.info(_('Started VM %s '), instance_name)
        except Exception as exn:
            LOG.exception(_('spawn vm failed: %s'), exn)
            self.destroy(instance)
            raise exn

    def _create_config_drive(self, instance, injected_files, admin_password):
        if CONF.config_drive_format != 'iso9660':
            vmutils.HyperVException(_('Invalid config_drive_format "%s"') %
                CONF.config_drive_format)

        LOG.info(_('Using config drive'), instance=instance)
        extra_md = {}
        if admin_password and CONF.config_drive_inject_password:
            extra_md['admin_pass'] = admin_password

        inst_md = instance_metadata.InstanceMetadata(instance,
            content=injected_files, extra_md=extra_md)

        instance_path = self._vmutils.get_instance_path(
            instance['name'])
        configdrive_path_iso = os.path.join(instance_path, 'configdrive.iso')
        LOG.info(_('Creating config drive at %(path)s'),
                 {'path': configdrive_path_iso}, instance=instance)

        cdb = configdrive.ConfigDriveBuilder(instance_md=inst_md)
        try:
            cdb.make_drive(configdrive_path_iso)
        finally:
            cdb.cleanup()

        if not CONF.config_drive_cdrom:
            drive_type = constants.IDE_DISK
            configdrive_path = os.path.join(instance_path,
                'configdrive.vhd')
            utils.execute(CONF.qemu_img_cmd,
                          'convert',
                          '-f',
                          'raw',
                          '-O',
                          'vpc',
                          configdrive_path_iso,
                          configdrive_path,
                          attempts=1)
            os.remove(configdrive_path_iso)
        else:
            drive_type = constants.IDE_DVD
            configdrive_path = configdrive_path_iso

        self._attach_ide_drive(instance['name'], configdrive_path, 1, 0,
            drive_type)

    def _create_vm(self, instance):
        """Create a VM but don't start it.  """
        instance_name = instance["name"]
        vs_man_svc = self._conn.Msvm_VirtualSystemManagementService()[0]

        vs_gs_data = self._conn.Msvm_VirtualSystemGlobalSettingData.new()
        vs_gs_data.ElementName = instance_name
        (job, ret_val) = vs_man_svc.DefineVirtualSystem(
                [], None, vs_gs_data.GetText_(1))[1:]
        if ret_val == constants.WMI_JOB_STATUS_STARTED:
            success = self._vmutils.check_job_status(job)
        else:
            success = (ret_val == 0)

        if not success:
            raise vmutils.HyperVException(_('Failed to create VM %s') %
                instance_name)

        LOG.debug(_('Created VM %s...'), instance_name)
        vm = self._conn.Msvm_ComputerSystem(ElementName=instance_name)[0]

        vmsettings = vm.associators(
                          wmi_result_class='Msvm_VirtualSystemSettingData')
        vmsetting = [s for s in vmsettings
                        if s.SettingType == 3][0]  # avoid snapshots
        memsetting = vmsetting.associators(
                           wmi_result_class='Msvm_MemorySettingData')[0]
        #No Dynamic Memory, so reservation, limit and quantity are identical.
        mem = long(str(instance['memory_mb']))
        memsetting.VirtualQuantity = mem
        memsetting.Reservation = mem
        memsetting.Limit = mem

        (job, ret_val) = vs_man_svc.ModifyVirtualSystemResources(
                vm.path_(), [memsetting.GetText_(1)])
        LOG.debug(_('Set memory for vm %s...'), instance_name)
        procsetting = vmsetting.associators(
                wmi_result_class='Msvm_ProcessorSettingData')[0]
        vcpus = long(instance['vcpus'])
        procsetting.VirtualQuantity = vcpus
        procsetting.Reservation = vcpus
        procsetting.Limit = 100000  # static assignment to 100%

        if CONF.limit_cpu_features:
            procsetting.LimitProcessorFeatures = True

        (job, ret_val) = vs_man_svc.ModifyVirtualSystemResources(
                vm.path_(), [procsetting.GetText_(1)])
        LOG.debug(_('Set vcpus for vm %s...'), instance_name)

    def _create_scsi_controller(self, vm_name):
        """ Create an iscsi controller ready to mount volumes """
        LOG.debug(_('Creating a scsi controller for %(vm_name)s for volume '
                'attaching') % locals())
        vms = self._conn.MSVM_ComputerSystem(ElementName=vm_name)
        vm = vms[0]
        scsicontrldefault = self._conn.query(
                "SELECT * FROM Msvm_ResourceAllocationSettingData \
                WHERE ResourceSubType = 'Microsoft Synthetic SCSI Controller'\
                AND InstanceID LIKE '%Default%'")[0]
        if scsicontrldefault is None:
            raise vmutils.HyperVException(_('Controller not found'))
        scsicontrl = self._vmutils.clone_wmi_obj(self._conn,
                'Msvm_ResourceAllocationSettingData', scsicontrldefault)
        scsicontrl.VirtualSystemIdentifiers = ['{' + str(uuid.uuid4()) + '}']
        scsiresource = self._vmutils.add_virt_resource(self._conn,
            scsicontrl, vm)
        if scsiresource is None:
            raise vmutils.HyperVException(
                _('Failed to add scsi controller to VM %s') %
                vm_name)

    def _get_ide_controller(self, vm, ctrller_addr):
        #Find the IDE controller for the vm.
        vmsettings = vm.associators(
            wmi_result_class='Msvm_VirtualSystemSettingData')
        rasds = vmsettings[0].associators(
            wmi_result_class='MSVM_ResourceAllocationSettingData')
        ctrller = [r for r in rasds
            if r.ResourceSubType == 'Microsoft Emulated IDE Controller'
            and r.Address == str(ctrller_addr)]
        return ctrller

    def _attach_ide_drive(self, vm_name, path, ctrller_addr, drive_addr,
        drive_type=constants.IDE_DISK):
        """Create an IDE drive and attach it to the vm"""
        LOG.debug(_('Creating disk for %(vm_name)s by attaching'
                ' disk file %(path)s') % locals())

        vms = self._conn.MSVM_ComputerSystem(ElementName=vm_name)
        vm = vms[0]

        ctrller = self._get_ide_controller(vm, ctrller_addr)

        if drive_type == constants.IDE_DISK:
            resSubType = 'Microsoft Synthetic Disk Drive'
        elif drive_type == constants.IDE_DVD:
            resSubType = 'Microsoft Synthetic DVD Drive'

        #Find the default disk drive object for the vm and clone it.
        drivedflt = self._conn.query(
            "SELECT * FROM Msvm_ResourceAllocationSettingData \
            WHERE ResourceSubType LIKE '%(resSubType)s'\
            AND InstanceID LIKE '%%Default%%'" % locals())[0]
        drive = self._vmutils.clone_wmi_obj(self._conn,
                'Msvm_ResourceAllocationSettingData', drivedflt)
        #Set the IDE ctrller as parent.
        drive.Parent = ctrller[0].path_()
        drive.Address = drive_addr
        #Add the cloned disk drive object to the vm.
        new_resources = self._vmutils.add_virt_resource(self._conn,
            drive, vm)
        if new_resources is None:
            raise vmutils.HyperVException(
                _('Failed to add drive to VM %s') %
                    vm_name)
        drive_path = new_resources[0]
        LOG.debug(_('New %(drive_type)s drive path is %(drive_path)s') %
            locals())

        if drive_type == constants.IDE_DISK:
            resSubType = 'Microsoft Virtual Hard Disk'
        elif drive_type == constants.IDE_DVD:
            resSubType = 'Microsoft Virtual CD/DVD Disk'

        #Find the default VHD disk object.
        drivedefault = self._conn.query(
                "SELECT * FROM Msvm_ResourceAllocationSettingData \
                 WHERE ResourceSubType LIKE '%(resSubType)s' AND \
                 InstanceID LIKE '%%Default%%' " % locals())[0]

        #Clone the default and point it to the image file.
        res = self._vmutils.clone_wmi_obj(self._conn,
                'Msvm_ResourceAllocationSettingData', drivedefault)
        #Set the new drive as the parent.
        res.Parent = drive_path
        res.Connection = [path]

        #Add the new vhd object as a virtual hard disk to the vm.
        new_resources = self._vmutils.add_virt_resource(self._conn, res, vm)
        if new_resources is None:
            raise vmutils.HyperVException(
                _('Failed to add %(drive_type)s image to VM %(vm_name)s') %
                    locals())
        LOG.info(_('Created drive type %(drive_type)s for %(vm_name)s') %
            locals())

    def _create_nic(self, vm_name, mac):
        """Create a (synthetic) nic and attach it to the vm"""
        LOG.debug(_('Creating nic for %s '), vm_name)
        #Find the vswitch that is connected to the physical nic.
        vms = self._conn.Msvm_ComputerSystem(ElementName=vm_name)
        extswitch = self._find_external_network()
        if extswitch is None:
            raise vmutils.HyperVException(_('Cannot find vSwitch'))

        vm = vms[0]
        switch_svc = self._conn.Msvm_VirtualSwitchManagementService()[0]
        #Find the default nic and clone it to create a new nic for the vm.
        #Use Msvm_SyntheticEthernetPortSettingData for Windows or Linux with
        #Linux Integration Components installed.
        syntheticnics_data = self._conn.Msvm_SyntheticEthernetPortSettingData()
        default_nic_data = [n for n in syntheticnics_data
                            if n.InstanceID.rfind('Default') > 0]
        new_nic_data = self._vmutils.clone_wmi_obj(self._conn,
                'Msvm_SyntheticEthernetPortSettingData',
                default_nic_data[0])
        #Create a port on the vswitch.
        (new_port, ret_val) = switch_svc.CreateSwitchPort(
            Name=str(uuid.uuid4()),
            FriendlyName=vm_name,
            ScopeOfResidence="",
            VirtualSwitch=extswitch.path_())
        if ret_val != 0:
            LOG.error(_('Failed creating a port on the external vswitch'))
            raise vmutils.HyperVException(_('Failed creating port for %s') %
                    vm_name)
        ext_path = extswitch.path_()
        LOG.debug(_("Created switch port %(vm_name)s on switch %(ext_path)s")
                % locals())
        #Connect the new nic to the new port.
        new_nic_data.Connection = [new_port]
        new_nic_data.ElementName = vm_name + ' nic'
        new_nic_data.Address = mac
        new_nic_data.StaticMacAddress = 'True'
        new_nic_data.VirtualSystemIdentifiers = ['{' + str(uuid.uuid4()) + '}']
        #Add the new nic to the vm.
        new_resources = self._vmutils.add_virt_resource(self._conn,
            new_nic_data, vm)
        if new_resources is None:
            raise vmutils.HyperVException(_('Failed to add nic to VM %s') %
                    vm_name)
        LOG.info(_("Created nic for %s "), vm_name)

    def _find_external_network(self):
        """Find the vswitch that is connected to the physical nic.
           Assumes only one physical nic on the host
        """
        #If there are no physical nics connected to networks, return.
        LOG.debug(_("Attempting to bind NIC to %s ")
                % CONF.vswitch_name)
        if CONF.vswitch_name:
            LOG.debug(_("Attempting to bind NIC to %s ")
                % CONF.vswitch_name)
            bound = self._conn.Msvm_VirtualSwitch(
                ElementName=CONF.vswitch_name)
        else:
            LOG.debug(_("No vSwitch specified, attaching to default"))
            self._conn.Msvm_ExternalEthernetPort(IsBound='TRUE')
        if len(bound) == 0:
            return None
        if CONF.vswitch_name:
            return self._conn.Msvm_VirtualSwitch(
                ElementName=CONF.vswitch_name)[0]\
                .associators(wmi_result_class='Msvm_SwitchPort')[0]\
                .associators(wmi_result_class='Msvm_VirtualSwitch')[0]
        else:
            return self._conn.Msvm_ExternalEthernetPort(IsBound='TRUE')\
                .associators(wmi_result_class='Msvm_SwitchPort')[0]\
                .associators(wmi_result_class='Msvm_VirtualSwitch')[0]

    def reboot(self, instance, network_info, reboot_type):
        instance_name = instance["name"]
        """Reboot the specified instance."""
        vm = self._vmutils.lookup(self._conn, instance_name)
        if vm is None:
            raise exception.InstanceNotFound(instance_id=instance["id"])
        self._set_vm_state(instance_name, 'Reboot')

    def destroy(self, instance, network_info=None, cleanup=True):
        """Destroy the VM. Also destroy the associated VHD disk files"""
        instance_name = instance["name"]
        LOG.debug(_("Got request to destroy vm %s"), instance_name)
        vm = self._vmutils.lookup(self._conn, instance_name)
        if vm is None:
            return
        vm = self._conn.Msvm_ComputerSystem(ElementName=instance_name)[0]
        vs_man_svc = self._conn.Msvm_VirtualSystemManagementService()[0]
        #Stop the VM first.
        self._set_vm_state(instance_name, 'Disabled')
        vmsettings = vm.associators(
                         wmi_result_class='Msvm_VirtualSystemSettingData')
        rasds = vmsettings[0].associators(
                         wmi_result_class='MSVM_ResourceAllocationSettingData')
        disks = [r for r in rasds
                    if r.ResourceSubType == 'Microsoft Virtual Hard Disk']
        disk_files = []
        volumes = [r for r in rasds
                    if r.ResourceSubType == 'Microsoft Physical Disk Drive']
        volumes_drives_list = []
        #collect the volumes information before destroying the VM.
        for volume in volumes:
            hostResources = volume.HostResource
            drive_path = hostResources[0]
            #Appending the Msvm_Disk path
            volumes_drives_list.append(drive_path)
        #Collect disk file information before destroying the VM.
        for disk in disks:
            disk_files.extend([c for c in disk.Connection])
        #Nuke the VM. Does not destroy disks.
        (job, ret_val) = vs_man_svc.DestroyVirtualSystem(vm.path_())
        if ret_val == constants.WMI_JOB_STATUS_STARTED:
            success = self._vmutils.check_job_status(job)
        elif ret_val == 0:
            success = True
        if not success:
            raise vmutils.HyperVException(_('Failed to destroy vm %s') %
                instance_name)
        #Disconnect volumes
        for volume_drive in volumes_drives_list:
            self._volumeops.disconnect_volume(volume_drive)
        #Delete associated vhd disk files.
        for disk in disk_files:
            vhdfile = self._conn_cimv2.query(
            "Select * from CIM_DataFile where Name = '" +
                disk.replace("'", "''") + "'")[0]
            LOG.debug(_("Del: disk %(vhdfile)s vm %(instance_name)s")
                % locals())
            vhdfile.Delete()

    def pause(self, instance):
        """Pause VM instance."""
        LOG.debug(_("Pause instance"), instance=instance)
        self._set_vm_state(instance["name"], 'Paused')

    def unpause(self, instance):
        """Unpause paused VM instance."""
        LOG.debug(_("Unpause instance"), instance=instance)
        self._set_vm_state(instance["name"], 'Enabled')

    def suspend(self, instance):
        """Suspend the specified instance."""
        print instance
        LOG.debug(_("Suspend instance"), instance=instance)
        self._set_vm_state(instance["name"], 'Suspended')

    def resume(self, instance):
        """Resume the suspended VM instance."""
        LOG.debug(_("Resume instance"), instance=instance)
        self._set_vm_state(instance["name"], 'Enabled')

    def power_off(self, instance):
        """Power off the specified instance."""
        LOG.debug(_("Power off instance"), instance=instance)
        self._set_vm_state(instance["name"], 'Disabled')

    def power_on(self, instance):
        """Power on the specified instance"""
        LOG.debug(_("Power on instance"), instance=instance)
        self._set_vm_state(instance["name"], 'Enabled')

    def _set_vm_state(self, vm_name, req_state):
        """Set the desired state of the VM"""
        vms = self._conn.Msvm_ComputerSystem(ElementName=vm_name)
        if len(vms) == 0:
            return False
        (job, ret_val) = vms[0].RequestStateChange(
            constants.REQ_POWER_STATE[req_state])
        success = False
        if ret_val == constants.WMI_JOB_STATUS_STARTED:
            success = self._vmutils.check_job_status(job)
        elif ret_val == 0:
            success = True
        elif ret_val == 32775:
            #Invalid state for current operation. Typically means it is
            #already in the state requested
            success = True
        if success:
            LOG.info(_("Successfully changed vm state of %(vm_name)s"
                    " to %(req_state)s") % locals())
        else:
            msg = _("Failed to change vm state of %(vm_name)s"
                    " to %(req_state)s") % locals()
            LOG.error(msg)
            raise vmutils.HyperVException(msg)

    def _cache_image(self, fn, target, fname, cow=False, Size=None,
        *args, **kwargs):
        """Wrapper for a method that creates an image that caches the image.

        This wrapper will save the image into a common store and create a
        copy for use by the hypervisor.

        The underlying method should specify a kwarg of target representing
        where the image will be saved.

        fname is used as the filename of the base image.  The filename needs
        to be unique to a given image.

        If cow is True, it will make a CoW image instead of a copy.
        """
        @lockutils.synchronized(fname, 'nova-')
        def call_if_not_exists(path, fn, *args, **kwargs):
                if not os.path.exists(path):
                    fn(target=path, *args, **kwargs)

        if not os.path.exists(target):
            LOG.debug(_("use_cow_image:%s"), cow)
            if cow:
                base = self._vmutils.get_base_vhd_path(fname)
                call_if_not_exists(base, fn, *args, **kwargs)

                image_service = self._conn.query(
                    "Select * from Msvm_ImageManagementService")[0]
                (job, ret_val) = \
                    image_service.CreateDifferencingVirtualHardDisk(
                        Path=target, ParentPath=base)
                LOG.debug(
                    "Creating difference disk: JobID=%s, Source=%s, Target=%s",
                    job, base, target)
                if ret_val == constants.WMI_JOB_STATUS_STARTED:
                    success = self._vmutils.check_job_status(job)
                else:
                    success = (ret_val == 0)

                if not success:
                    raise vmutils.HyperVException(
                        _('Failed to create Difference Disk from '
                            '%(base)s to %(target)s') % locals())

            else:
                call_if_not_exists(target, fn, *args, **kwargs)
