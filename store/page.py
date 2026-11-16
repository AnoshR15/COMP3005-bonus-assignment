"""Slotted page: variable-length records in one 4096-byte page.

    offset  size  field
         0     4  page_id       uint32
         4     2  slot_count    uint16   includes dead slots
         6     2  free_end      uint16   lowest byte offset used by record data; 4096 when empty
         8     4  slot 0        uint16 offset, uint16 length
        12     4  slot 1
       ...
                                <-- free space -->
     free_end                   record data, newest at the lowest offset
      4095                      end of page

All integers are little endian. The slot directory grows forward from byte 8, record data grows
backward from the end.

A slot with offset 0 is dead: its record was deleted. Slot numbers are never reused and never
renumbered, because a record id (page id, slot number) must stay valid.

    free space = free_end - (8 + 4 * slot_count)

An insert of a record of L bytes needs L + 4 bytes of free space: the record and its new slot.
Deleting a record frees nothing. There is no compaction (DESIGN_M1.md says why).

The functions take the page as a bytearray and change it in place.
"""
import struct
from .disk import PAGE_SIZE

HEADER = struct.Struct('<IHH')      # page_id, slot_count, free_end
SLOT = struct.Struct('<HH')         # offset, length
HEADER_SIZE = HEADER.size           # 8
SLOT_SIZE = SLOT.size               # 4
CAPACITY = PAGE_SIZE - HEADER_SIZE  # 4088: the free space of an empty page
NO_PAGE = 0xFFFFFFFF


class PageError(Exception):
    pass


def init(buf, page_id):
    """Make buf an empty slotted page."""
    buf[:] = bytes(PAGE_SIZE)
    HEADER.pack_into(buf, 0, page_id, 0, PAGE_SIZE)


def header(buf):
    """(page_id, slot_count, free_end)"""
    return HEADER.unpack_from(buf, 0)


def slot_count(buf):
    return HEADER.unpack_from(buf, 0)[1]


def free_space(buf):
    _, count, free_end = HEADER.unpack_from(buf, 0)
    return free_end - (HEADER_SIZE + SLOT_SIZE * count)


def insert(buf, record):
    """Store a record and return its slot number, or None when there is no space."""
    page_id, count, free_end = HEADER.unpack_from(buf, 0)
    length = len(record)
    if free_end - (HEADER_SIZE + SLOT_SIZE * count) < length + SLOT_SIZE:
        return None
    free_end -= length
    buf[free_end:free_end + length] = record
    SLOT.pack_into(buf, HEADER_SIZE + SLOT_SIZE * count, free_end, length)
    HEADER.pack_into(buf, 0, page_id, count + 1, free_end)
    return count


def get(buf, slot):
    """The bytes of the record in a slot. An error if there is no such slot or it is dead."""
    if not 0 <= slot < slot_count(buf):
        raise PageError(f'slot {slot} does not exist; the page has {slot_count(buf)} slots')
    offset, length = SLOT.unpack_from(buf, HEADER_SIZE + SLOT_SIZE * slot)
    if offset == 0:
        raise PageError(f'slot {slot} is dead')
    return bytes(buf[offset:offset + length])


def delete(buf, slot):
    """Mark a slot dead. Its bytes stay where they are; the space is not reclaimed."""
    if not 0 <= slot < slot_count(buf):
        raise PageError(f'slot {slot} does not exist; the page has {slot_count(buf)} slots')
    at = HEADER_SIZE + SLOT_SIZE * slot
    if SLOT.unpack_from(buf, at)[0] == 0:
        raise PageError(f'slot {slot} is already dead')
    SLOT.pack_into(buf, at, 0, 0)


def slots(buf):
    """(slot number, offset, length) for every slot; offset 0 marks a dead one."""
    for i in range(slot_count(buf)):
        offset, length = SLOT.unpack_from(buf, HEADER_SIZE + SLOT_SIZE * i)
        yield i, offset, length


def records(buf):
    """(slot number, record bytes) for every live record, in slot order."""
    view = bytes(buf)
    for i in range(slot_count(view)):
        offset, length = SLOT.unpack_from(view, HEADER_SIZE + SLOT_SIZE * i)
        if offset:
            yield i, view[offset:offset + length]
