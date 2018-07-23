#!/usr/bin/python3
# -*- coding: utf-8 -*-

# Dependencies for Ubuntu 16.04
# - python3-cairocffi
# - python3-dbus
# - python3-evdev >= 0.7.1 (needs backporting)
# - python3-pyudev
# - python3-systemd
# - python3-xcffib

import re
import os
import sys
import subprocess
import time
from collections import OrderedDict

# Logging modules
import logging
from systemd.journal import JournalHandler

# XCB window creation and drawing modules
import xcffib
from xcffib.xproto import (Atom, CW, ConfigWindow, PropMode, WindowClass)
import cairocffi

# Input device handling modules
import asyncio
import pyudev
import evdev

# DBus modules (for communication with systemd-logind)
import dbus

MAX_SEAT_COUNT = 5
XORG_CONF_DIR = '/etc/X11/xorg.conf.d'
SCREENS_DIR = '/usr/share/oi-lab-multi-seat-config/screens'
LOGIND_PATH = 'org.freedesktop.login1'
LOGIND_OBJECT = '/org/freedesktop/login1'
LOGIND_INTERFACE = 'org.freedesktop.login1.Manager'

bus = dbus.SystemBus()
logind = dbus.Interface(bus.get_object(LOGIND_PATH, LOGIND_OBJECT),
                        dbus_interface=LOGIND_INTERFACE)

logger = logging.getLogger(sys.argv[0])
logger.setLevel(logging.INFO)
logger.propagate = False
stdout_handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(
    '%(asctime)s %(name)s[%(process)s] %(levelname)s %(message)s')
stdout_handler.setFormatter(formatter)
logger.addHandler(stdout_handler)
logger.addHandler(JournalHandler())


def get_seat_name():
    try:
        return os.environ['XDG_SEAT']
    except KeyError:
        # The environment variable XDG_SEAT is not set for us.
        # Let's infer the seat name by filtering arguments "-seat seatFoo"
        # from this display's Xorg full command line
        output = subprocess.check_output(
            ['ps', '--no-headers', '-o', 'cmd', '-C', 'Xorg']
        ).decode('utf-8').strip().split('\n')

        for line in output:
            if os.environ['DISPLAY'].split('.')[0] in line:
                for arg in line.split(' '):
                    if arg.startswith('seat'):
                        return arg


def rotate(list_to_rotate, amount):
    return list_to_rotate[-amount:] + list_to_rotate[:-amount]


def find_root_visual(screen):
    """Find the xproto.VISUALTYPE corresponding to the root visual"""
    for i in screen.allowed_depths:
        for v in i.visuals:
            if v.visual_id == screen.root_visual:
                return v


class Window:
    def __init__(self):
        logger.info('Connecting to X server %s', os.environ['DISPLAY'])
        self.connection = xcffib.connect()
        self.id = self.connection.generate_id()

        screen = self.connection.get_setup().roots[self.connection.pref_screen]

        # Initialize window geometry as full screen size
        self.x = 0
        self.y = 0
        self.width = screen.width_in_pixels
        self.height = screen.height_in_pixels

        # Create window
        self.connection.core.CreateWindow(xcffib.CopyFromParent,
                                          self.id,
                                          screen.root,
                                          self.x, self.y,
                                          self.width, self.height,
                                          0,
                                          WindowClass.InputOutput,
                                          screen.root_visual,
                                          CW.BackPixel,
                                          [screen.white_pixel])

        # Show window
        self.connection.core.MapWindow(self.id)

        # Set Cairo surface and context
        xcb_surface = cairocffi.XCBSurface(self.connection,
                                           self.id,
                                           find_root_visual(screen),
                                           self.width,
                                           self.height)
        self.context = cairocffi.Context(xcb_surface)

        self.connection.flush()

    def set_wm_name(self, name):
        self.name = name
        self.connection.core.ChangeProperty(PropMode.Replace,
                                            self.id,
                                            Atom.WM_NAME,
                                            Atom.STRING,
                                            8,
                                            len(name),
                                            name)

    def load_image(self, image_path):
        self.connection.core.ClearArea(False, self.id, 0, 0, 0, 0)

        image_surface = cairocffi.ImageSurface.create_from_png(image_path)
        image_width = image_surface.get_width()
        image_height = image_surface.get_height()

        self.context.set_source_rgb(0, 0.533333333, 0.666666667)
        self.context.paint()

        self.context.set_source_surface(image_surface,
                                        (self.width-image_width) / 2,
                                        (self.height-image_height) / 2)
        self.context.paint()

        self.connection.flush()

    def write_message(self, message):
        self.context.select_font_face('sans-serif')
        self.context.set_font_size(24)
        self.context.set_source_rgb(1, 1, 1)
        self.context.move_to(10, 30)
        self.context.show_text(message)

        self.connection.flush()


class SeatDevice:
    def __init__(self, device):
        self.device_path = device.device_path
        self.device_node = device.device_node
        self.sys_path = device.sys_path
        self.sys_name = device.sys_name
        self.seat_name = device.properties.get('ID_SEAT')

        parent = device.find_parent('pci')
        self.pci_slot = (parent.properties['PCI_SLOT_NAME'].lstrip('0000:')
                         if parent is not None else None)

    def attach_to_seat(self, seat_name):
        try:
            logind.AttachDevice(seat_name, self.sys_path, False)
            self.seat_name = seat_name

            # Sometimes the new udev rules are not automatically loaded
            # after calling systemd-logind's AttachDevice() method,
            # so we'll force it here.
            subprocess.run(['udevadm', 'control', '--reload-rules'])
            subprocess.run(['udevadm', 'trigger'])

            logger.info('Device %s successfully attached to seat %s',
                        self.sys_path, seat_name)
        except Exception as error:
            logger.error('Failed to attach device %s to seat %s!',
                         self.sys_path, seat_name)
            logger.error(error)


class SeatHubDevice(SeatDevice):
    def __init__(self, device):
        super().__init__(device)

        try:
            self.product_id = device.attributes.asstring('idProduct')
        except:
            self.product_id = None

        try:
            self.vendor_id = device.attributes.asstring('idVendor')
        except:
            self.vendor_id = None


class SeatInputDevice(SeatDevice):
    def __init__(self, device):
        def is_root_hub(device):
            # all root hubs have the same manufacturer 1d6b (Linux Foundation)
            try:
                return device.attributes.asstring('idVendor') == '1d6b'
            except:
                return False

        def get_parent_hub(device):
            parent = device.find_parent('usb', device_type='usb_device')
            return (None
                    if parent is None or is_root_hub(parent)
                    else (SeatHubDevice(parent)
                          if 'seat' in parent.tags
                          else get_parent_hub(parent)))

        super().__init__(device)

        # Only real USB hubs are allowed here!
        self.parent = get_parent_hub(device)

    def attach_to_seat(self, seat_name):
        if self.parent is not None:
            # If input device is connected to a USB hub,
            # attach the hub to the seat instead, so that
            # all other devices connected to the same hub
            # will be automatically attached to the same seat.
            self.parent.attach_to_seat(seat_name)
        else:
            super().attach_to_seat(seat_name)


def scan_keyboard_devices(context):
    devices = context.list_devices(subsystem='input', ID_INPUT_KEYBOARD=True)
    return [SeatInputDevice(device)
            for device in devices
            if device.device_node is not None]


def scan_mouse_devices(context):
    devices = context.list_devices(subsystem='input',
                                   ID_INPUT_MOUSE=True,
                                   sys_name='event*')
    return [SeatInputDevice(device)
            for device in devices
            if device.device_node is not None]


def main():
    context = pyudev.Context()
    all_keyboard_devices = scan_keyboard_devices(context)
    all_mouse_devices = scan_mouse_devices(context)
    this_seat_name = get_seat_name()
    logger.info('This seat name: %s', this_seat_name)

    # Here, configured_seats[seat_name] is
    # - True, if seat_name is seat0 or if there's at least one keyboard
    #   attached to seat_name, or
    # - False, otherwise.
    d = {seat_name: (seat_name == 'seat0'
                     or bool([device
                              for device in all_keyboard_devices
                              if device.seat_name == seat_name]))
         for (seat_name, _) in logind.ListSeats()}

    # We'll rotate the keys here to place seat0 in the first index
    configured_seats = OrderedDict(
        rotate(sorted(d.items(), key=lambda item: item[0]), 1))

    if configured_seats[this_seat_name]:
        # There's already a keyboard attached to this seat. We can exit now.
        return

    # Collect all input devices which are not yet attached to any seat
    available_keyboard_devices = [device
                                  for device in all_keyboard_devices
                                  if device.seat_name is None]
    available_mouse_devices = [device
                               for device in all_mouse_devices
                               if device.seat_name is None]

    for device in available_keyboard_devices:
        logger.info('Available keyboard detected: %s -> %s',
                    device.device_node, device.sys_path)

        if device.parent is not None:
            logger.info('>>> Parent device: %s', device.parent.sys_path)

    for device in available_mouse_devices:
        logger.info('Available mouse detected: %s -> %s',
                    device.device_node, device.sys_path)

        if device.parent is not None:
            logger.info('>>> Parent device: %s', device.parent.sys_path)

    if len(available_keyboard_devices) <= 1:
        # There are not enough keyboards available to attach to this seat
        # (if there's only one keyboard available, it will be reserved
        # to seat0), so we can exit now.
        logger.info(
            'No input devices available for %s. Seat configuration aborted.',
            this_seat_name)
        sys.exit(1)

    # Put this in a list, so it can be used globally in coroutines
    num_available_keyboards = [len(available_keyboard_devices)]

    # Initialize window
    window = Window()
    window.set_wm_name('oi-lab-multi-seat-window-{}'.format(this_seat_name))
    window.load_image('{}/wait-loading.png'.format(SCREENS_DIR))
    time.sleep(1)

    loop = asyncio.get_event_loop()

    async def read_all_keys(loop, keyboard):
        def refresh_screen(loop):
            this_seat_name = get_seat_name()
            seat_names = [*configured_seats.keys()]
            seat_states = [*configured_seats.values()] + [None] * \
                (MAX_SEAT_COUNT - len(seat_names))

            index = seat_names.index(this_seat_name)
            status = ''.join(str(int(bool(state)))
                             for state in seat_states[1:])

            image_path = '{}/seat{}-{}.png'.format(SCREENS_DIR, index, status)
            logger.info('Loading image %s', image_path)
            window.load_image(image_path)

            remaining_seats = min(seat_states.count(False),
                                  num_available_keyboards[0] - 1)
            window.write_message(
                'Terminais restantes: {}        Teclados disponíveis: {}'.format(
                    remaining_seats, num_available_keyboards[0]))

            if configured_seats[this_seat_name]:
                loop.stop()
            elif remaining_seats == 0:
                logger.info(
                    'No more input devices available for %s. Seat configuration aborted.',
                    this_seat_name)
                time.sleep(1)
                sys.exit(1)

        refresh_screen(loop)

        # EV_KEY event values: 0 (release), 1 (press), or 2 (hold)
        async def read_key(keyboard):
            device = evdev.InputDevice(keyboard.device_node)

            async for event in device.async_read_loop():
                # pylint: disable=no-member
                if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                    return event.code - evdev.ecodes.KEY_F1 + 1

        valid_key_pressed = False

        while not valid_key_pressed:
            key = await read_key(keyboard)
            valid_key_pressed = (1 <= key <= len(configured_seats)
                                 and not [*configured_seats.values()][key])

        logger.info('Key F%d pressed on keyboard %s',
                    key, keyboard.device_node)
        key_seat_name = [*configured_seats.keys()][key]

        if (key_seat_name == get_seat_name()):
            # This is the key we expect! Attach the keyboard to this seat.
            keyboard.attach_to_seat(key_seat_name)

            # If the keyboard being attached to this seat is connected
            # directly to a computer's USB or PS/2 port, find a mouse
            # connected directly to another USB or PS/2 port
            # and attach it together.
            #
            # Please note that, in this case,
            # - if there's more than one mouse in this condition,
            #   we can't ensure which one will be attached;
            # - this seat will have no access to audio and removable media.
            if keyboard.parent is None:
                root_mouse_devices = [device
                                      for device in all_mouse_devices
                                      if device.parent is None]

                if root_mouse_devices:
                    root_mouse_devices[0].attach_to_seat(key_seat_name)

        configured_seats[key_seat_name] = True
        num_available_keyboards[0] -= 1
        refresh_screen(loop)

    coroutines = (read_all_keys(loop, keyboard)
                  for keyboard in available_keyboard_devices)
    future = asyncio.gather(*coroutines)

    try:
        loop.run_until_complete(future)
    except RuntimeError as error:
        # It's OK to stop the loop if there still are
        # unfinished coroutines and this seat was just configured.
        if not configured_seats[this_seat_name]:
            raise error

    logger.info('Configuration finished for %s.', this_seat_name)
    time.sleep(1)


if __name__ == '__main__':
    main()
