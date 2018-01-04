# Copyright (C) 2008, 2009 Canonical Ltd.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3,
# as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import stat
import sys
import shutil
from usbcreator.misc import (
    USBCreatorProcessException,
    callable,
    popen,
    )
from threading import Thread, Event
import logging
from hashlib import md5
from usbcreator.misc import MAX_DBUS_TIMEOUT


class install(Thread):
    def __init__(self, source, target, device=None,
                 allow_system_internal=False):
        Thread.__init__(self)
        self.source = source
        self.target = target
        self.device = device
        self.allow_system_internal = allow_system_internal
        self._stopevent = Event()
        logging.debug('install thread source: %s' % source)
        logging.debug('install thread target: %s' % target)

    # Signals.

    def success(self):
        pass
    
    def _success(self):
        if callable(self.success):
            self.success()

    def failure(self, message=None):
        pass

    def _failure(self, message=None):
        logging.critical(message)
        if callable(self.failure):
            self.failure(message)
        sys.exit(1)

    def progress(self, complete):
        '''Emitted with an integer percentage of progress completed, time
        remaining, and speed.'''
        pass

    def progress_message(self, message):
        '''Emitted with a translated string like "Installing the
        bootloader..."
        '''
        pass

    def retry(self, message):
        '''Will be called when we need to know if the user wants to try a
        failed operation again.  Must return a boolean value.'''
        pass
    
    def join(self, timeout=None):
        self._stopevent.set()
        Thread.join(self, timeout)

    def check(self):
        if self._stopevent.isSet():
            logging.debug('Asked by the controlling thread to shut down.')
            sys.exit(0)
    
    # Exception catching wrapper.

    def run(self):
        try:
            if os.path.isfile(self.source):
                ext = os.path.splitext(self.source)[1].lower()
                if ext not in ['.iso', '.img']:
                    self._failure(_('The extension "%s" is not supported.') %
                                    ext)
                self.diskimage_install()
            else:
                self.diskimage_install()
            self._success()
        except Exception as e:
            # TODO evand 2009-07-25: Bring up our own apport-like utility.
            logging.exception('Exception raised:')
            self._failure(_('An uncaught exception was raised:\n%s') % str(e))

    # Helpers for core routines.
    def diskimage_install(self):
        self.progress_message(_('Writing disk image...'))
        failure_msg = _('Could not write the disk image (%(source)s) to the device'
                        ' (%(device)s).') % {'source': self.source,
                                             'device': self.device}
        
        import dbus
        try:
            bus = dbus.SystemBus()
            obj = bus.get_object('com.ubuntu.USBCreator',
                                 '/com/ubuntu/USBCreator')
            obj.Image(self.source, self.device, self.allow_system_internal,
                      dbus_interface='com.ubuntu.USBCreator',
                      timeout=MAX_DBUS_TIMEOUT)
        except dbus.DBusException:
            self._failure(failure_msg)

