# Copyright (C) 2008-2009 Canonical Ltd.

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

import subprocess
import os

import gettext
import logging
from gi.repository import GObject
from gi.repository import GLib
from gi.repository import Gio
from gi.repository import Gdk
from gi.repository import Pango
from gi.repository import Gtk

from usbcreator.frontends.base import Frontend
from usbcreator.frontends.gtk.unitysupport import UnitySupport
from usbcreator import misc

if 'USBCREATOR_LOCAL' in os.environ:
    ui_path = os.path.join(os.getcwd(), 'gui/usbcreator-gtk.ui')
else:
    ui_path = '/usr/share/usb-creator/usbcreator-gtk.ui'

Gdk.threads_init()

def thread_wrap(func):
    '''Decorator for functions that will be called by another thread.'''
    def wrap(*args):
        Gdk.threads_enter()
        try:
            return func(*args)
        finally:
            Gdk.threads_leave()
    return wrap

class GtkFrontend(Frontend):
    @classmethod
    def startup_failure(cls, message):
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE, message_format=message)
        dialog.run()
        dialog.destroy()

    @classmethod
    def DBusMainLoop(cls):
        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)

    def __init__(self, backend, img=None, allow_system_internal=False):

        self.allow_system_internal = allow_system_internal

        self.all_widgets = set()

        self.builder = Gtk.Builder()
        self.builder.set_translation_domain('usbcreator')
        self.builder.add_from_file(ui_path)
        # A cache of the icon names and display names for the individual
        # devices.
        self.icons = {}
        self.names = {}
        self.pretty_names = {}

        for widget in self.builder.get_objects():
            # Taken from ubiquity:
            # We generally want labels to be selectable so that people can
            # easily report problems in them
            # (https://launchpad.net/bugs/41618), but GTK+ likes to put
            # selectable labels in the focus chain, and I can't seem to turn
            # this off in glade and have it stick. Accordingly, make sure
            # labels are unfocusable here.
            if isinstance(widget, Gtk.Label):
                widget.set_property('can-focus', False)
            if issubclass(type(widget), Gtk.Widget):
                self.all_widgets.add(widget)
                widget.set_name(Gtk.Buildable.get_name(widget))
                setattr(self, Gtk.Widget.get_name(widget), widget)

        Gtk.Window.set_default_icon_name('usb-creator-gtk')

        # Connect signals to widgets
        self.builder.connect_signals (self)
        self.cancelbutton.connect('clicked', lambda x: self.warning_dialog.hide())
        self.finished_exit.connect('clicked', lambda x: self.finished_dialog.hide())
        self.failed_exit.connect('clicked', lambda x: self.failed_exit.hide())
        self.progress_cancel_button.connect('clicked', lambda x: self.warning_dialog.show())

        # Set warning dialog transient window
        self.warning_dialog.set_transient_for(self.install_window)

        def format_value(scale, value):
            return misc.format_mb_size(value)

        # TODO: remove format button
        self.format_dest.hide()

        # Connect to backend signals.
        self.backend = backend
        self.backend.source_added_cb = self.add_source
        self.backend.target_added_cb = self.add_target
        self.backend.source_removed_cb = self.remove_source
        self.backend.target_removed_cb = self.remove_target
        self.backend.failure_cb = self.failure
        self.backend.success_cb = self.success
        self.backend.install_progress_cb = self.progress
        self.backend.install_progress_message_cb = self.progress_message
        self.backend.install_progress_pulse_cb = self.progress_pulse
        self.backend.install_progress_pulse_stop_cb = self.progress_pulse_stop
        self.backend.retry_cb = self.retry
        self.backend.target_changed_cb = self.update_target

        # Pulse state.
        self.pulsing = False

        # we currently do not have help
        self.button_help.hide()
        #self.button_help.connect('clicked', lambda x: Gtk.show_uri(self.button_help.get_screen(),
        #                                                           'ghelp:usb-creator',
        #                                                           Gtk.get_current_event_time()))
            
        self.setup_sources_treeview()
        self.setup_targets_treeview()

        # Pre-populate the source view.
        if img is not None:
            self.backend.add_image(img)
            self.source_vbox.hide()

        for search_dir_id in [GLib.UserDirectory.DIRECTORY_DOWNLOAD, GLib.UserDirectory.DIRECTORY_DESKTOP]:
            search_dir = GLib.get_user_special_dir(search_dir_id)
            if search_dir and os.path.isdir(search_dir):
                # TODO evand 2009-10-22: File type detection based on file(1).
                for fname in os.listdir(search_dir):
                    if fname.endswith('.iso') or fname.endswith('.img'):
                        self.backend.add_image(os.path.join(search_dir, fname))

        # Sets first pre-populated image as current in the backend
        self.selection_changed_source(self.source_treeview.get_selection())

        self.window.show()
        selection = self.source_treeview.get_selection()
        selection.connect('changed', self.selection_changed_source)
        selection = self.dest_treeview.get_selection()
        selection.connect('changed', self.selection_changed_target)

        self.backend.detect_devices()
        # FIXME: instead of passing parent we really should just send signals
        self.unity = UnitySupport(parent=self)
        self.update_loop = self.add_timeout(2000, self.backend.update_free)
        Gdk.threads_enter()
        try:
            Gtk.main()
        except KeyboardInterrupt:
            self.quit()
        Gdk.threads_leave()

    def add_timeout(self, interval, func, *args):
        '''Add a new timer for function 'func' with optional arguments. Wraps a
        similar gobject call timeout_add.'''

        timer = GLib.timeout_add(interval, func, *args)

        return timer
    def delete_timeout(self, timer):
        '''Remove the specified timer. Wraps gobject source_remove call.'''

        return GLib.source_remove(timer)

    def add_source(self, source):
        logging.debug('add_source: %s' % str(source))
        _append_to_list_and_select(self.source_treeview, [source],
            force_selection=True)

        # XXX evand 2009-09-17: Find a label and icon for the device, if we
        # don't already have one.  This will go away when the code for the
        # presentation-name and presentation-icon properties in udisks
        # is written.
        t = self.backend.sources[source]['type']
        l = self.backend.sources[source]['label']
        d = self.backend.sources[source]['device']
        if not (t == misc.SOURCE_ISO or t == misc.SOURCE_IMG):
            name, icon = self.get_gnome_drive(d)
            if icon:
                self.icons[source] = icon
            if not l and name:
                self.names[source] = name

    def add_target(self, target):
        logging.debug('add_target: %s' % str(target))
        # use str() here, since it is originally a DBus.ObjectPath object
        _append_to_list_and_select(self.dest_treeview, [str(target)],
            force_selection=False)

        # XXX evand 2009-09-17: Find a label and icon for the device.
        v = self.backend.targets[target]['vendor']
        m = self.backend.targets[target]['model']
        d = self.backend.targets[target]['device']
        l = self.backend.targets[target]['label']
        name, icon = self.get_gnome_drive(d)
        if icon:
            self.icons[target] = icon
        if not l and name:
            self.names[target] = name
        self.pretty_names[target] = "%s %s (%s)" % (v, m, misc.text_type(d))

    def remove_source(self, source):
        model = self.source_treeview.get_model()
        iterator = model.get_iter_first()
        to_delete = None
        while iterator is not None:
            if model.get_value(iterator, 0) == source:
                to_delete = iterator
            iterator = model.iter_next(iterator)
        if to_delete is not None:
            model.remove(to_delete)

        if source in self.names:
            self.names.pop(source)
        if source in self.icons:
            self.icons.pop(source)

        sel = self.source_treeview.get_selection()
        m, i = sel.get_selected()

    def remove_target(self, target):
        model = self.dest_treeview.get_model()
        iterator = model.get_iter_first()
        to_delete = None
        while iterator is not None:
            if model.get_value(iterator, 0) == target:
                to_delete = iterator
            iterator = model.iter_next(iterator)
        if to_delete is not None:
            model.remove(to_delete)

        if target in self.names:
            self.names.pop(target)
        if target in self.icons:
            self.icons.pop(target)
        if target in self.pretty_names:
            self.pretty_names.pop(target)

        sel = self.dest_treeview.get_selection()
        m, i = sel.get_selected()

    def get_source(self):
        '''Returns the UDI of the selected source image.'''
        sel = self.source_treeview.get_selection()
        m, i = sel.get_selected()
        if i:
            return m[i][0]
        else:
            logging.debug('No source selected.')
            return ''

    def get_target(self):
        '''Returns the UDI of the selected target disk or partition.'''
        sel = self.dest_treeview.get_selection()
        m, i = sel.get_selected()
        if i:
            return m[i][0]
        else:
            logging.debug('No target selected.')
            return ''

    def get_gnome_drive(self, dev):
        try:
            monitor = Gio.VolumeMonitor.get()
            for drive in monitor.get_volumes():
                if 'unix-device' in drive.enumerate_identifiers():
                    if drive.get_identifier('unix-device') == dev:
                        name = drive.get_name()
                        icon = drive.get_icon().get_names()[0]
                        return (name, icon)
            for drive in monitor.get_connected_drives():
                if 'unix-device' in drive.enumerate_identifiers():
                    if drive.get_identifier('unix-device') == dev:
                        name = drive.get_name()
                        icon = drive.get_icon().get_names()[0]
                        return (name, icon)
        except Exception:
            logging.exception('Could not determine GNOME drive:')
        return ('', '')

    def setup_sources_treeview(self):
        def column_data_func(layout, cell, model, iterator, column):
            if not self.backend:
                return
            udi = model[iterator][0]
            dev = self.backend.sources[udi]
            if column == 0:
                if udi in self.names:
                    cell.set_property('text', self.names[udi])
                else:
                    cell.set_property('text', dev['device'])
            elif column == 1:
                cell.set_property('text', dev['label'])
            elif column == 2:
                cell.set_property('text', misc.format_size(dev['size']))

        def pixbuf_data_func(column, cell, model, iterator, data):
            if not self.backend:
                return
            udi = model[iterator][0]
            dev = self.backend.sources[udi]
            source_type = dev['type']
            if source_type == misc.SOURCE_ISO:
                cell.set_property('stock-id', Gtk.STOCK_CDROM)
            elif source_type == misc.SOURCE_IMG:
                cell.set_property('stock-id', Gtk.STOCK_HARDDISK)
            else:
                if udi in self.icons:
                    cell.set_property('icon-name', self.icons[udi])
                else:
                    cell.set_property('stock-id', None)

        list_store = Gtk.ListStore(str)
        self.source_treeview.set_model(list_store)

        cell_name = Gtk.CellRendererText()
        cell_name.set_property('ellipsize', Pango.EllipsizeMode.END)
        cell_pixbuf = Gtk.CellRendererPixbuf()
        column_name = Gtk.TreeViewColumn(_('CD-Drive/Image'))
        column_name.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column_name.set_resizable(True)
        column_name.set_expand(True)
        column_name.set_min_width(75)
        column_name.pack_start(cell_pixbuf, False)
        column_name.pack_start(cell_name, True)
        self.source_treeview.append_column(column_name)
        column_name.set_cell_data_func(cell_name, column_data_func, 0)
        column_name.set_cell_data_func(cell_pixbuf, pixbuf_data_func, None)

        cell_version = Gtk.CellRendererText()
        cell_version.set_property('ellipsize', Pango.EllipsizeMode.END)
        column_name = Gtk.TreeViewColumn(_('OS Version'), cell_version)
        column_name.set_cell_data_func(cell_version, column_data_func, 1)
        column_name.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column_name.set_resizable(True)
        column_name.set_expand(True)
        column_name.set_min_width(75)
        self.source_treeview.append_column(column_name)

        cell_size = Gtk.CellRendererText()
        cell_size.set_property('ellipsize', Pango.EllipsizeMode.END)
        column_name = Gtk.TreeViewColumn(_('Size'), cell_size)
        column_name.set_cell_data_func(cell_size, column_data_func, 2)
        column_name.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column_name.set_resizable(True)
        column_name.set_expand(False)
        column_name.set_min_width(75)
        self.source_treeview.append_column(column_name)

        # Drag and drop support.
        # FIXME evand 2009-04-28: Anything can be dropped on the source
        # treeview.  Ideally, the user should only be able to drop ISO and IMG
        # files.

        def motion_cb(wid, context, x, y, time):
            context.drag_status(Gdk.DragAction.COPY, time)
            return True

        def drop_cb(w, context, x, y, time):
            target_list = w.drag_dest_get_target_list()
            target = w.drag_dest_find_target(context, target_list)
            w.drag_get_data(context, target)
            context.finish(True, True)
            return True

        def data_received_cb(w, context, x, y, selection, target_type, timestamp):
            # FIXME evand 2009-04-28: Use the GNOME VFS?  Test with a sshfs
            # nautilus window.
            file = selection.data.strip('\r\n\x00')
            if file.startswith('file://'):
                file = file[7:]
            elif file.startswith('file:'):
                file = file[5:]
            self.backend.add_image(file)

        self.source_treeview.drag_dest_set(Gtk.DestDefaults.ALL,
            [Gtk.TargetEntry.new('text/uri-list', 0, 600)], Gdk.DragAction.COPY)
        self.source_treeview.connect('drag_motion', motion_cb)
        self.source_treeview.connect('drag_drop', drop_cb)
        self.source_treeview.connect('drag-data-received', data_received_cb)

    def update_target(self, udi):
        m = self.dest_treeview.get_model()
        iterator = m.get_iter_first()
        # Update the warning / error icon in the treeview.
        while iterator is not None:
            if m.get_value(iterator, 0) == udi:
                m.row_changed(m.get_path(iterator), iterator)
                break
            iterator = m.iter_next(iterator)

        target = self.backend.targets[udi]

        # Update install button state.
        status = target['status']
        source = self.backend.get_current_source()
        if not source:
            self.button_install.set_sensitive(False)
            return
        if status == misc.CAN_USE:
            self.button_install.set_sensitive(True)
        else:
            self.button_install.set_sensitive(False)
        # Update the destination status message.
        self.open_dest.hide()
        if status == misc.CANNOT_USE:
            msg = _('The device is not large enough to hold this image.')
        else:
            msg = ''
        self.dest_status.set_text(msg)

    def selection_changed_source(self, selection):
        model, iterator = selection.get_selected()
        if not iterator:
            return
        udi = model[iterator][0]
        self.backend.set_current_source(udi)
        self.selection_changed_target(self.dest_treeview.get_selection())

    def selection_changed_target(self, selection):
        model, iterator = selection.get_selected()
        if not iterator:
            return
        udi = model[iterator][0]
        if udi:
            self.update_target(udi)

        dev = self.backend.targets[udi]
        p = dev['parent']
        if p and p in self.backend.targets:
            dev = self.backend.targets[p]

    def setup_targets_treeview(self):
        def column_data_func(layout, cell, model, iterator, column):
            if not self.backend:
                return
            udi = model[iterator][0]
            dev = self.backend.targets[udi]

            if column == 0:
                if udi in self.pretty_names:
                    cell.set_property('text', self.pretty_names[udi])
                else:
                    cell.set_property('text', dev['device'])
            elif column == 1:
                if udi in self.names:
                    cell.set_property('text', self.names[udi])
                else:
                    cell.set_property('text', dev['label'])
            elif column == 2:
                cell.set_property('text', misc.format_size(dev['capacity']))

        def pixbuf_data_func(column, cell, model, iterator, data):
            if not self.backend:
                return
            udi = model[iterator][0]
            dev = self.backend.targets[udi]
            status = dev['status']

            if status == misc.NEED_SPACE:
                cell.set_property('stock-id', Gtk.STOCK_DIALOG_WARNING)
            elif status == misc.CANNOT_USE:
                # TODO evand 2009-05-05: Implement disabled rows as a
                # replacement?
                cell.set_property('stock-id', Gtk.STOCK_DIALOG_ERROR)
            else:
                if udi in self.icons:
                    cell.set_property('icon-name', self.icons[udi])
                else:
                    cell.set_property('stock-id', None)

        list_store = Gtk.ListStore(GObject.TYPE_STRING)
        list_store.set_sort_column_id(0, Gtk.SortType.ASCENDING)
        self.dest_treeview.set_model(list_store)

        column_name = Gtk.TreeViewColumn()
        column_name.set_title(_('Device'))
        cell_name = Gtk.CellRendererText()
        cell_name.set_property('ellipsize', Pango.EllipsizeMode.END)
        cell_pixbuf = Gtk.CellRendererPixbuf()
        column_name.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column_name.set_resizable(True)
        column_name.set_expand(True)
        column_name.set_min_width(75)
        column_name.pack_start(cell_pixbuf, False)
        column_name.pack_start(cell_name, True)
        self.dest_treeview.append_column(column_name)
        column_name.set_cell_data_func(cell_name, column_data_func, 0)
        column_name.set_cell_data_func(cell_pixbuf, pixbuf_data_func, None)

        cell_name = Gtk.CellRendererText()
        cell_name.set_property('ellipsize', Pango.EllipsizeMode.END)
        column_name = Gtk.TreeViewColumn(_('Label'), cell_name)
        column_name.set_cell_data_func(cell_name, column_data_func, 1)
        column_name.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column_name.set_resizable(True)
        column_name.set_expand(True)
        column_name.set_min_width(75)
        self.dest_treeview.append_column(column_name)

        cell_capacity = Gtk.CellRendererText()
        cell_capacity.set_property('ellipsize', Pango.EllipsizeMode.END)
        column_name = Gtk.TreeViewColumn(_('Capacity'), cell_capacity)
        column_name.set_cell_data_func(cell_capacity, column_data_func, 2)
        column_name.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column_name.set_resizable(True)
        column_name.set_expand(False)
        column_name.set_min_width(75)
        self.dest_treeview.append_column(column_name)

    def add_file_source_dialog(self, *args):
        filename = ''
        chooser = Gtk.FileChooserDialog(title=None, parent=self.window,
                                        action=Gtk.FileChooserAction.OPEN,
                                        buttons=(Gtk.STOCK_CANCEL,
                                                 Gtk.ResponseType.CANCEL,
                                                 Gtk.STOCK_OPEN,
                                                 Gtk.ResponseType.OK))
        def _add_filter(p, n):
            filter = Gtk.FileFilter()
            filter.add_pattern(p)
            filter.set_name(n)
            chooser.add_filter(filter)

        _add_filter('*.iso', _('CD Images'))
        _add_filter('*.img', _('Disk Images'))            

        folder = os.path.expanduser('~')
        chooser.set_current_folder(folder)
        response = chooser.run()
        if response == Gtk.ResponseType.OK:
            filename = chooser.get_filename()
            chooser.destroy()
            treeview = self.source_treeview
            model = treeview.get_model()
            iterator = model.get_iter_first()
            while iterator is not None:
                if model.get_value(iterator, 0) == filename:
                    path = model.get_path(iterator)
                    treeview.set_cursor(path, None, False)
                    treeview.scroll_to_cell(path)
                    return
                iterator = model.iter_next(iterator)
            self.backend.add_image(filename)
        else:
            chooser.destroy()

    def install(self, widget):
        source = self.get_source()
        target = self.get_target()
        if source and target:
            d = Gtk.MessageDialog(transient_for=self.window, modal=True,
                    message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO)
            d.set_markup(_('Are you sure you want to write the disc image to the device?'))
            d.format_secondary_markup(_('All existing data will be lost.'))
            response = d.run()
            d.destroy()
            if response != Gtk.ResponseType.YES:
                return

            starting_up = _('Starting up...')
            self.progress_title.set_markup('<big><b>' + starting_up + '</b></big>')
            self.progress_info.set_text('')

            self.install_window.show()
            self.window.hide()
            self.unity.show_progress()
            self.delete_timeout(self.update_loop)
            try:
                self.backend.install(source, target,
                                     allow_system_internal=self.allow_system_internal)
            except:
                self._fail()

    @thread_wrap
    def progress(self, complete):
        if self.pulsing:
            self.progress_info.set_text('')
            return
        # Ahem.
        if complete > 100:
            complete = 100
        self.progress_bar.set_fraction(complete / 100.0)
        self.unity.set_progress(complete / 100.0)
        if complete < 100:
            self.progress_info.set_text(_('%d%% complete') % complete)
        else:
            self.progress_info.set_text(_('Finishing...'))

    @thread_wrap
    def progress_message(self, message):
        self.progress_title.set_markup('<big><b>' + message + '</b></big>')

    @thread_wrap
    def progress_pulse(self):
        def pulse():
            self.progress_bar.pulse()
            return True
        self.pulsing = self.add_timeout(100, pulse)

    @thread_wrap
    def progress_pulse_stop(self):
        if self.pulsing:
            self.delete_timeout(self.pulsing)
            self.pulsing = False

    @thread_wrap
    def retry(self, message):
        retry_dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, destroy_with_parent=True,
                                         message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.YES_NO,
                                         message_format=message)
        response = retry_dialog.run()
        retry_dialog.destroy()
        return response == Gtk.ResponseType.YES

    def quit(self, *args):
        self.backend.cancel_install()
        if Gtk.main_level() > 0:
            Gtk.main_quit()

    @thread_wrap
    def failure(self, message=None):
        self._fail(message)

    def _fail(self, message=None):
        logging.exception('Installation failed.')
        # FIXME: evand 2009-07-28: Do we need this?
        self.warning_dialog.hide()
        self.install_window.hide()
        self.unity.show_progress(False)
        if not message:
            message = _('Installation failed.')
        self.failed_dialog.set_transient_for(self.install_window)
        self.failed_dialog_label.set_text(message)
        self.failed_dialog.run()
        Gtk.main_quit()

    @thread_wrap
    def success(self):
        self.warning_dialog.hide()
        self.install_window.hide()
        self.unity.show_progress(False)
        try:
            import dbus
            bus = dbus.SystemBus()
            obj = bus.get_object('com.ubuntu.USBCreator',
                                 '/com/ubuntu/USBCreator')
            if obj.KVMOk(dbus_interface='com.ubuntu.USBCreator',
                         timeout=misc.MAX_DBUS_TIMEOUT):
                self.kvm_test.show()
                def go(*args):
                    obj.KVMTest(self.get_target(), os.environ,
                                dbus_interface='com.ubuntu.USBCreator')
                self.kvm_test.connect('clicked', go)
        except dbus.DBusException:
            logging.exception('Error checking for kvm:')

        self.finished_dialog.set_transient_for(self.install_window)
        self.finished_dialog.run()
        self.backend.shutdown()
        Gtk.main_quit()

    def notify(self, message):
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, destroy_with_parent=True,
                                   message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.CLOSE,
                                   message_format=message)
        dialog.run()
        dialog.destroy()

    def format_dest_clicked(self, *args):
        return

    def open_dest_folder(self, *args):
        model, iterator = self.dest_treeview.get_selection().get_selected()
        if not iterator:
            logging.error('Open button pressed but there was no selection.')
            return
        disk = model[iterator][0]
        dir = self.backend.open(disk)
        if dir:
            subprocess.Popen(['gnome-open', dir])

def _append_to_list_and_select(treeview, new_row, force_selection):
    model = treeview.get_model()
    new_iter = model.append(new_row)
    if force_selection or (treeview.get_selection().get_selected()[1] is None):
        treeview.set_cursor(model.get_path(new_iter), None, False)
        treeview.scroll_to_cell(model.get_path(new_iter))

# vim: set ai et sts=4 tabstop=4 sw=4:
