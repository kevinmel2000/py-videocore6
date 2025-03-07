
import sys
import mmap
from videocore6.drm_v3d import DRM_V3D
from videocore6.assembler import Assembly
import numpy as np


DEFAULT_CODE_AREA_SIZE = 1024 * 1024
DEFAULT_DATA_AREA_SIZE = 32 * 1024 * 1024


class DriverError(Exception):
    pass


class Array(np.ndarray):

    def __new__(cls, *args, **kwargs):

        phyaddr = kwargs.pop('phyaddr')
        obj = super().__new__(cls, *args, **kwargs)
        obj.address = phyaddr
        return obj

    def addresses(self):

        return np.arange(
                self.address,
                self.address + self.nbytes,
                self.itemsize,
                np.uint32,
        ).reshape(self.shape)


class Memory(object):

    def __init__(self, drm, size):

        self.drm = drm
        self.size = size
        self.handle  = None  # Handle of BO for V3D DRM
        self.phyaddr = None  # Physical address used in QPU
        self.buffer  = None  # mmap object of the memory area

        try:

            self.handle, self.phyaddr = drm.v3d_create_bo(size)
            offset = drm.v3d_mmap_bo(self.handle)
            self.buffer = mmap.mmap(fileno = drm.fd, length = size,
                    flags = mmap.MAP_SHARED,
                    prot = mmap.PROT_READ | mmap.PROT_WRITE,
                    offset = offset)

        except Exception as e:

            self.close()
            raise e

    def close(self):

        if self.buffer is not None:
            self.buffer.close()

        if self.handle is not None:
            self.drm.gem_close(self.handle)

        self.drm = None
        self.size = None
        self.handle = None
        self.phyaddr = None
        self.buffer = None


class Driver(object):

    def __init__(self, *,
            code_area_size = DEFAULT_CODE_AREA_SIZE,
            data_area_size = DEFAULT_DATA_AREA_SIZE,
    ):

        self.code_area_size = code_area_size
        self.data_area_size = data_area_size
        total_size = self.code_area_size + self.data_area_size
        self.code_area_base = 0
        self.data_area_base = self.code_area_base + self.code_area_size
        self.code_pos = self.code_area_base
        self.data_pos = self.data_area_base

        self.drm = None
        self.memory = None
        self.bo_handles = None

        try:

            self.drm = DRM_V3D()

            self.memory = Memory(self.drm, total_size)

            self.handles = np.array([self.memory.handle], dtype = np.uint32)
            self.bo_handles = self.handles.ctypes.data

        except Exception as e:

            self.close()
            raise e

    def close(self):

        if self.memory is not None:
            self.memory.close()

        if self.drm is not None:
            self.drm.close()

        self.drm = None
        self.memory = None
        self.handles = None
        self.bo_handles = None

    def __enter__(self):

        return self

    def __exit__(self, exc_type, value, traceback):

        self.close()
        return exc_type is None

    def alloc(self, *args, **kwargs):

        offset = self.data_pos
        kwargs['phyaddr'] = self.memory.phyaddr + offset
        kwargs['buffer'] = self.memory.buffer
        kwargs['offset'] = offset

        arr = Array(*args, **kwargs)

        self.data_pos += arr.nbytes
        if self.data_pos > self.data_area_base + self.data_area_size:
            raise DriverError('Data too large')

        return arr

    def dump_program(self, prog, *args, **kwargs):
        file = kwargs.pop('file') if 'file' in kwargs else sys.stdout
        asm = Assembly()
        prog(asm, *args, **kwargs)
        asm.finalize()
        for insn in asm:
            print(f'{int(insn):#018x}', file = file)

    def program(self, prog, *args, **kwargs):

        asm = Assembly()
        prog(asm, *args, **kwargs)
        asm.finalize()
        asm = [int(x) for x in asm]

        offset = self.code_pos
        code = Array(
                shape = len(asm),
                dtype = np.uint64,
                phyaddr = self.memory.phyaddr + offset,
                buffer = self.memory.buffer,
                offset = offset,
        )

        self.code_pos += code.nbytes
        if self.code_pos > self.code_area_base + self.code_area_size:
            raise DriverError('Code too large')

        code[:] = asm

        return code

    def execute(self, code, uniforms = None, timeout_sec = 10000):

        self.drm.v3d_submit_csd(
                cfg = [
                    # WGS X, Y, Z and settings
                    0, 0, 0, 0,
                    # Number of batches minus 1
                    0,
                    # Shader address, pnan, singleseg, threading
                    code.addresses()[0],
                    # Uniforms address
                    uniforms if uniforms is not None else 0,
                ],
                # Not used in the driver.
                coef = [0, 0, 0, 0],
                bo_handles = self.bo_handles,
                bo_handle_count = len(self.handles),
                in_sync = 0,
                out_sync = 0,
        )

        # XXX: Separate function
        for handle in self.handles:
            self.drm.v3d_wait_bo(handle, timeout_ns = int(timeout_sec / 1e-9))
