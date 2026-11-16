"""Buffer pool: a fixed array of B frames, and nothing in memory that is not in one of them.

Each frame holds one page's bytes, a pin count and a dirty flag. The page table maps
(file name, page id) to the index of the frame holding that page.

    fetch_page    resident: pin it again                                   -> a HIT
                  not resident: take a free frame, or choose a victim among the frames whose
                  pin count is zero; write the victim out if it is dirty (a WRITE); read the
                  page from disk (a READ)                                  -> a MISS,
                  and an EVICTION if a resident page was replaced
    unpin_page    an error if the page is not resident or its pin count is already zero
    new_page      a new page at the end of a file: zero filled, pinned, dirty
    flush_all     write every dirty frame, clear the flags

If every frame is pinned, fetch fails with an error. A pinned page is never evicted.

Replacement policy, chosen at start:

    lru     every fetch, hit or miss, makes that frame the most recently used; the victim is
            the least recently used frame whose pin count is zero
    clock   the frames are a circle with a reference bit each and a hand. Every fetch, hit or
            miss, sets the frame's bit. A newly loaded page has its bit set and the hand moves
            past it. To find a victim the hand walks: a pinned frame is skipped, a frame with
            its bit set has the bit cleared and is passed, the first unpinned frame with a
            clear bit is the victim.

One addition for the execution engine: reserve(n) takes n frames out of the pool as working
memory for an operator (a sort's run, a hash table, the page of a temporary file). A reserved
frame is as untouchable as a pinned one. frames_in_use() is pinned frames plus reserved frames,
and high_water remembers its maximum.
"""
from .disk import PAGE_SIZE

RESERVED = 'reserved'


class BufferPoolError(Exception):
    pass


class Frame:
    __slots__ = ('file', 'page_id', 'data', 'pin_count', 'dirty', 'referenced', 'last_used')

    def __init__(self, file, page_id, data):
        self.file = file
        self.page_id = page_id
        self.data = data            # bytearray of PAGE_SIZE bytes
        self.pin_count = 0
        self.dirty = False
        self.referenced = False
        self.last_used = 0


class BufferPool:

    def __init__(self, disk, frames, policy='lru'):
        if frames < 1:
            raise BufferPoolError('a buffer pool needs at least one frame')
        if policy not in ('lru', 'clock'):
            raise BufferPoolError(f"unknown replacement policy '{policy}'; expected lru or clock")
        self.disk = disk
        self.size = frames
        self.policy = policy
        self.frames = [None] * frames       # None (free), a Frame, or RESERVED
        self.page_table = {}                # (file, page id) -> index into frames
        self.reserved = []                  # indexes of reserved frames, most recent last
        self.hand = 0                       # clock hand
        self.time = 0                       # counts accesses, for LRU
        self.pinned = 0                     # frames whose pin count is above zero
        self.high_water = 0
        self.hits = self.misses = self.evictions = self.reads = self.writes = 0

    # ---------------------------------------------------------------- pages

    def fetch_page(self, file, page_id, counters=None):
        """Return the frame holding the page, pinned. `counters`, if given, is charged the disk
        read when there is one (the execution engine's per-operator page_reads)."""
        index = self.page_table.get((file, page_id))
        if index is not None:
            self.hits += 1
            frame = self.frames[index]
        else:
            index = self._take_frame()
            frame = Frame(file, page_id, bytearray(self.disk.read_page(file, page_id)))
            self.reads += 1
            self.misses += 1
            if counters is not None:
                counters.page_reads += 1
            self._install(index, frame)
        self._pin(frame)
        return frame

    def new_page(self, file):
        """Allocate a page at the end of a file and return its frame: zeroed, pinned, dirty."""
        index = self._take_frame()
        frame = Frame(file, self.disk.allocate_page(file), bytearray(PAGE_SIZE))
        frame.dirty = True
        self._install(index, frame)
        self._pin(frame)
        return frame

    def unpin_page(self, file, page_id, is_dirty=False):
        index = self.page_table.get((file, page_id))
        if index is None:
            raise BufferPoolError(f'cannot unpin page {page_id} of {file}: it is not in the pool')
        frame = self.frames[index]
        if frame.pin_count == 0:
            raise BufferPoolError(f'cannot unpin page {page_id} of {file}: its pin count is already 0')
        frame.dirty = frame.dirty or is_dirty
        frame.pin_count -= 1
        if frame.pin_count == 0:
            self.pinned -= 1

    def flush_all(self):
        for frame in self.frames:
            if isinstance(frame, Frame) and frame.dirty:
                self._write(frame)

    def stats(self):
        return {'hits': self.hits, 'misses': self.misses, 'evictions': self.evictions,
                'reads': self.reads, 'writes': self.writes}

    # ---------------------------------------------------------------- working memory

    def reserve(self, count):
        """Take `count` frames out of the pool for an operator. All or nothing."""
        taken = 0
        try:
            for _ in range(count):
                index = self._take_frame()
                self.frames[index] = RESERVED
                self.reserved.append(index)
                taken += 1
        except BufferPoolError:
            self.release(taken)
            raise
        self._note_use()

    def release(self, count):
        for _ in range(count):
            self.frames[self.reserved.pop()] = None

    def frames_in_use(self):
        return self.pinned + len(self.reserved)

    # ---------------------------------------------------------------- internals

    def _install(self, index, frame):
        self.frames[index] = frame
        self.page_table[(frame.file, frame.page_id)] = index
        if self.policy == 'clock':
            self.hand = (index + 1) % self.size     # the hand moves past a newly loaded page

    def _pin(self, frame):
        self.time += 1
        frame.last_used = self.time                 # LRU: now the most recently used
        frame.referenced = True                     # CLOCK: reference bit set by every fetch
        frame.pin_count += 1
        if frame.pin_count == 1:
            self.pinned += 1
            self._note_use()

    def _note_use(self):
        if self.frames_in_use() > self.high_water:
            self.high_water = self.frames_in_use()

    def _write(self, frame):
        self.disk.write_page(frame.file, frame.page_id, bytes(frame.data))
        self.writes += 1
        frame.dirty = False

    def _take_frame(self):
        """Index of a frame that can take a new page: a free one, else the victim's after it has
        been written out (if dirty) and removed from the page table."""
        for index, frame in enumerate(self.frames):
            if frame is None:
                return index
        index = self._lru_victim() if self.policy == 'lru' else self._clock_victim()
        if index is None:
            raise BufferPoolError(f'no unpinned frame available: all {self.size} frames are pinned'
                                  + (' or reserved' if self.reserved else ''))
        frame = self.frames[index]
        if frame.dirty:
            self._write(frame)
        del self.page_table[(frame.file, frame.page_id)]
        self.frames[index] = None
        self.evictions += 1
        return index

    def _lru_victim(self):
        best = None
        for index, frame in enumerate(self.frames):
            if isinstance(frame, Frame) and frame.pin_count == 0:
                if best is None or frame.last_used < self.frames[best].last_used:
                    best = index
        return best

    def _clock_victim(self):
        swept = 0
        while True:
            index = self.hand
            frame = self.frames[index]
            self.hand = (self.hand + 1) % self.size
            if frame is RESERVED or frame.pin_count > 0:
                swept += 1
            elif frame.referenced:
                frame.referenced = False
                swept += 1
            else:
                return index
            if swept > 2 * self.size:
                return None                         # every frame is pinned
