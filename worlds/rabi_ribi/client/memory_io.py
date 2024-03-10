"""
This module defines a class RabiRibiMemoryIO, which attaches
itself to an open Rabi Ribi process and reads/writes memory
in realtime. This is used to:
 - Recognize when a player gets an item
 - Write items to the player's inventory
 - Handle in game flags when a player recieves an AP item.
 - Modify map data loaded into memory.
 - etc... 
"""
import asyncio
import struct
import psutil

from pymem import pymem
import keystone

from CommonClient import logger

OFFSET_AREA_ID = int(0xD9CF88)
OFFSET_PLAYER_X = int(0x0103469C)
OFFSET_PLAYER_Y = int(0x013AFDB4)
OFFSET_GIVE_ITEM_FUNC = int(0x15A90)
OFFSET_PLAYER_FROZEN = int(0x1031DDC)
OFFSET_ITEM_MAP_0 = int(0xDFFB3C)
OFFSET_INVENTORY_EXCLAMATION_POINT = int(0x1673050)
OFFSET_INVENTORY_START = int(0x1672FA4)
OFFSET_MAX_HEALTH = int(0x16E6D24)
OFFSET_EGG_COUNT = int(0x1675CCC)
TILE_LENGTH = 64

class RabiRibiMemoryIO():
    """
    RabiRibiMemoryIO serves as an interface for reading/writing memory to and from
    a rabi-ribi game instance.
    """

    def __init__(self):
        self.rr_mem = None
        self.rr_process_id = None
        self.addr_injected_give_item_entrypoint = None

    def is_connected(self):
        if self.rr_mem is None:
            return False
        if not psutil.pid_exists(self.rr_process_id):  # confirm the process is still running
            logger.info("Rabi Ribi connection lost.")
            self.rr_process_id = None
            self.rr_mem = None
            return False
        return True

    async def connect(self, exit_event: asyncio.Event):
        logger.info("Waiting for connection to Rabi Ribi game instance...")
        while not exit_event.is_set():
            try:
                self.rr_mem = pymem.Pymem("rabiribi.exe")
                self.rr_process_id = self.rr_mem.process_id
                self.allocate()
                logger.info("Successfully connected to Rabi Ribi Game.")
                return
            except pymem.exception.ProcessNotFound:
                await asyncio.sleep(3)

    def allocate(self):
        self.addr_injected_give_item_entrypoint = self.rr_mem.allocate(12)

    def _read_word(self, offset):
        """
        Read 4 bytes of data at <base_process_address> + offset and return it.

        :int offset: the offset to read data from.
        :returns: The data represented as a byte string
        """
        data = self.rr_mem.read_bytes(self.rr_mem.base_address + offset, 4)
        return data

    def _read_float(self, offset):
        """
        Read a word at the specified offset, and interpret it as a float.

        :int offset: the offset to read data from.
        :returns: The data represented as a float.
        """
        data = self._read_word(offset)
        return struct.unpack("f", data)[0]

    def _read_int(self, offset):
        """
        Read a word at the specified offset, and interpret it as an int.

        :int offset: the offset to read data from.
        :returns: The data represented as a float.
        """
        data = self._read_word(offset)
        return struct.unpack("i", data)[0]

    def _read_bool(self, offset):
        """
        Read a word at the specified offset, and interpret it as a bool

        :int offset: the offset to read data from.
        :returns: The data represented as a float.
        """
        data = self._read_word(offset)
        if (struct.unpack("i", data)[0] == 0):
            return False
        return True

    def read_player_tile_position(self):
        """
        Read the player (area_id,x,y) and convert it to tile (area_id,x,y).

        :returns: The tile position represented as an integer 2-tuple
        """
        area_id = self._read_int(OFFSET_AREA_ID)
        player_x = self._read_float(OFFSET_PLAYER_X)
        player_y = self._read_float(OFFSET_PLAYER_Y)

        # Round to nearest tile
        if (player_x % TILE_LENGTH >= (TILE_LENGTH / 2)):
            player_x += (TILE_LENGTH / 2)
        if (player_y % TILE_LENGTH >= (TILE_LENGTH / 2)):
            player_y += (TILE_LENGTH / 2)

        return (int(area_id), int(player_x // TILE_LENGTH), int(player_y // TILE_LENGTH))

    def is_player_frozen(self):
        """
        Returns True if the player is frozen. This is a way of checking if we (potentially)
        just got an item.
        """
        return self._read_bool(OFFSET_PLAYER_FROZEN) or not self._read_bool(OFFSET_MAX_HEALTH)

    def give_item(self, item_id):
        """
        Run the in-game give-item function. We do this by injecting our own code
        which calls the func (setting the correct parameters in the registers),
        and then running a thread at the startpoint of our injected code.

        :int item_id: the rabi-ribi id of the item to give to the player
        :returns: None
        """
        addr_give_item_func = self.rr_mem.base_address + OFFSET_GIVE_ITEM_FUNC
        # keystone interprets injected entrypoint as 0. We subtract the injected entrypoint address
        #   so we can math the actual offset we want from the real 0 addr.
        addr_give_item_func = addr_give_item_func - self.addr_injected_give_item_entrypoint

        # convert our assembly code to bytes
        architecture = keystone.KS_ARCH_X86
        mode = keystone.KS_MODE_32
        endianess = keystone.KS_MODE_LITTLE_ENDIAN
        ks = keystone.Ks(architecture, mode | endianess)
        injected_call_func_code = f"mov ecx,{item_id}; call {addr_give_item_func}; ret".encode()
        injected_call_func_code, _ = ks.asm(injected_call_func_code)
        injected_call_func_code = bytes(injected_call_func_code)

        # write our code to memory
        self.rr_mem.write_bytes(
            self.addr_injected_give_item_entrypoint, 
            injected_call_func_code, 
            len(injected_call_func_code)
        )

        # start a thread at the entrypoint of our injected code
        self.rr_mem.start_thread(self.addr_injected_give_item_entrypoint)

    def remove_item_from_in_memory_map(self, area_id: int, x: int, y: int):
        """
        This method sets a specific tile on the map loaded into memory to having no item
        on it. This is used to delete items from the map when the player collects an item
        for another world.

        :int area_id: the area id of the location
        :int x: the x coordinate of the item
        :int y: the y coordinate of the item
        """
        map_tile_item_info_offset = (
            self.rr_mem.base_address +
            OFFSET_ITEM_MAP_0 +
            (area_id * 200000) +
            (((x * 200) + y) * 2)
        )
        self.rr_mem.write_bytes(
            map_tile_item_info_offset,
            b'\x00\x00',
            2
        )

    def remove_exclamation_point_from_inventory(self):
        """
        This removes the exclamation point item (the item that represents other worlds' items
        to false, and takes it away from the player after its recieved)
        """
        self.rr_mem.write_bytes(
            self.rr_mem.base_address + OFFSET_INVENTORY_EXCLAMATION_POINT,
            b'\x00\x00\x00\x00',
            4
        )

    def does_player_have_item_id(self, item_id) -> bool:
        """
        Returns true if player currently has item_id in their inventory.
        """
        return self._read_bool(OFFSET_INVENTORY_START + (4 * int(item_id)))

    def get_number_of_eggs_collected(self) -> int:
        """
        Returns the number of eggs the player currently has
        """
        return self._read_int(OFFSET_EGG_COUNT)
