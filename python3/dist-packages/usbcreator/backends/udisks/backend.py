import dbus
import logging
from dbus.mainloop.glib import DBusGMainLoop, threads_init
from gi.repository import Gio, GLib, UDisks
from usbcreator.backends.base import Backend
from usbcreator import misc

loop_prefix = '/org/freedesktop/UDisks2/block_devices/loop'

not_interesting = (
    '/org/freedesktop/UDisks2/block_devices/dm_',
    '/org/freedesktop/UDisks2/block_devices/ram',    
    '/org/freedesktop/UDisks2/block_devices/zram',
    '/org/freedesktop/UDisks2/drives/', 
    )

import time

class UDisksBackend(Backend):
    def __init__(self, allow_system_internal=False, bus=None):
        Backend.__init__(self)
        self.handles = []
        self.allow_system_internal = allow_system_internal
        logging.debug('UDisks2Backend')
        DBusGMainLoop(set_as_default=True)
        threads_init()
        if bus:
            self.bus = bus
        else:
            self.bus = dbus.SystemBus()

        self.udisks = UDisks.Client.new_sync(None)

        self.helper = self.bus.get_object('com.ubuntu.USBCreator',
                                          '/com/ubuntu/USBCreator')
        self.helper = dbus.Interface(self.helper, 'com.ubuntu.USBCreator')
        self.helper.connect_to_signal('Progress', self.got_progress)
        self.no_options = GLib.Variant('a{sv}', {})

    # Adapted from udisk's test harness.
    # This is why the entire backend needs to be its own thread.
    def retry_mount(self, fs):
        '''Try to mount until it does not fail with "Busy".'''
        timeout = 10
        while timeout >= 0:
            try:
                return fs.call_mount_sync(self.no_options, None)
            except GLib.GError as e:
                if not 'UDisks2.Error.DeviceBusy' in e.message:
                    raise
                logging.debug('Busy.')
                time.sleep(0.3)
                timeout -= 1
        return ''

    def got_progress(self, complete):
        self.install_progress_cb(complete)

    # Device detection and processing functions.
    def detect_devices(self):
        '''Start looking for new devices to add.  Devices added will be sent to
        the fronted using frontend.device_added.  Devices will only be added as
        they arrive if a main loop is present.'''
        logging.debug('detect_devices')
        # TODO connect add/remove objects + changed interface signals
        self.manager = self.udisks.get_object_manager()
        self.handles += [self.manager.connect('object-added', lambda man, obj: self._udisks_obj_added(obj))]
        self.handles += [self.manager.connect('object-removed', lambda man, obj: self._device_removed(obj.get_object_path()))]
        self.handles += [self.manager.connect('interface-added', lambda man, obj, iface: self._device_changed(obj))]
        self.handles += [self.manager.connect('interface-removed', lambda man, obj, iface: self._device_changed(obj))]
        self.handles += [self.manager.connect('interface-proxy-properties-changed', lambda man, obj, iface, props, invalid: self._device_changed(obj))]
        for obj in self.manager.get_objects():
            self._udisks_obj_added(obj)

    def _udisks_obj_added(self, obj):
        path = obj.get_object_path()
        for boring in not_interesting:
            if path.startswith(boring):
                return

        block = obj.get_block()
        if not block:
            return
        
        drive_name = block.get_cached_property('Drive').get_string()
        if drive_name != '/':
            drive = self.udisks.get_object(drive_name).get_drive()
        else:
            drive = None
            
        if drive and drive.get_cached_property('Optical').get_boolean():
            return

        part = obj.get_partition()
        is_system = block.get_cached_property('HintSystem').get_boolean()
        is_loop = path.startswith(loop_prefix)
        if self.allow_system_internal or not (is_system or is_loop):
            if part:
                return
            else:
                self._udisks_drive_added(obj, block, drive, path)
            
    def _udisks_drive_added(self, obj, block, drive, path):
        logging.debug('drive added: %s' % path)

        if drive:
            vendor = drive.get_cached_property('Vendor').get_string()
            model = drive.get_cached_property('Model').get_string()
            size = block.get_cached_property('Size').get_uint64()
            label = block.get_cached_property('IdLabel').get_string()
        else:
            vendor = ''
            model = ''
            size = block.get_cached_property('Size').get_uint64()
            label = block.get_cached_property('IdLabel').get_string()

        if size <= 0:
            logging.debug('not adding device: 0 byte disk.')
            return

        self.targets[path] = {
            'vendor': vendor,
            'model' : model,
            'label' : label,
            'free'  : -1,
            'device': block.get_cached_property('Device').get_bytestring().decode('utf-8'),
            'capacity' : size,
            'status' : misc.NEED_FORMAT,
            'mountpoint' : None,
            'parent' : None,
        }
        if misc.callable(self.target_added_cb):
            self.target_added_cb(path)
        self.update_free()
            
    def _device_changed(self, obj):
        path = obj.get_object_path()
        logging.debug('device change %s' % path)
        # As this will happen in the same event, the frontend wont change
        # (though it needs to make sure the list is sorted, otherwise it will).
        self._device_removed(path)
        self._udisks_obj_added(obj)

    # Device manipulation functions.
    def _is_casper_cd(self, filename):
        for search in ['/.disk/info', '/.disk/mini-info']:
            cmd = ['isoinfo', '-J', '-i', filename, '-x', search]
            try:
                output = misc.popen(cmd, stderr=None)
                if output:
                    return output
            except misc.USBCreatorProcessException:
                # TODO evand 2009-07-26: Error dialog.
                logging.error('Could not extract .disk/info.')
        return None

    def install(self, source, target, allow_system_internal=False):
        # TODO evand 2009-07-31: Lock source and target.
        logging.debug('install source: %s' % source)
        logging.debug('install target: %s' % target)

        # There's no going back now...
        for handle in self.handles:
            self.manager.disconnect(handle)

        dev = self.targets[target]['device']
        self.helper.Unmount(target)

        Backend.install(self, source, target, device=dev,
                        allow_system_internal=allow_system_internal)

    def cancel_install(self):
        Backend.cancel_install(self)

    def shutdown(self):
        try:
            self.helper.Shutdown()
        except GLib.GError as e:
            logging.exception('Could not shut down the dbus service.')
