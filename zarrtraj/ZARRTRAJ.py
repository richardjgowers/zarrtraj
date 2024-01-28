import numpy as np
import MDAnalysis as mda
from MDAnalysis.coordinates import base, core
from MDAnalysis.exceptions import NoDataError
from MDAnalysis.due import due, Doi
from MDAnalysis.lib.util import store_init_arguments


try:
    import zarr
except ImportError:
    HAS_ZARR = False

    # Allow building documentation even if zarr is not installed
    import types

    class MockZarrFile:
        pass
    zarr = types.ModuleType("zarr")
    zarr.File = MockZarrFile

else:
    HAS_ZARR = True


class ZarrTrajReader(base.ReaderBase):

    format = 'ZARRTRAJ'

    @store_init_arguments
    def __init__(self, filename,
                 **kwargs):
        
        if not HAS_ZARR:
            raise RuntimeError("Please install zarr")
        super(ZarrTrajReader, self).__init__(filename, **kwargs)
        self.filename = filename
        # NOTE: Not yet implemented
        # self.convert_units = convert_units 

        self.open_trajectory()
        if self._particle_group['box'].attrs['dimension'] != 3:
            raise ValueError("MDAnalysis only supports 3-dimensional"
                             " simulation boxes")
        
        # _has dictionary used for checking whether zarrtraj file has
        # 'position', 'velocity', or 'force' groups in the file
        self._has = {name: name in self._particle_group for
                     name in ('position', 'velocity', 'force')} 
        
        # Gets some info about what settings the datasets were created with
        # from first available group
        for name, value in self._has.items():
            if value:
                dset = self._particle_group[f'{name}/value']
                self.n_atoms = dset.shape[1]
                self.compressor = dset.compressor
                break
        else:
            raise NoDataError("Provide at least a position, velocity"
                              " or force group in the h5md file.")
        
        self.ts = self._Timestep(self.n_atoms,
                                 positions=self.has_positions,
                                 velocities=self.has_velocities,
                                 forces=self.has_forces,
                                 **self._ts_kwargs)
        
        self.units = {'time': None,
                      'length': None,
                      'velocity': None,
                      'force': None}
        # self._set_translated_units()  # fills units dictionary NOTE not yet implemented
        self._read_next_timestep() 

    @staticmethod
    def _format_hint(thing):
        """Can this Reader read *thing*"""
        # Check if the object is already a zarr.Group
        # If it isn't, try opening it as a group and if it excepts, return False
        if not HAS_ZARR:
            return False
      
        if isinstance(thing, zarr.Group):
            return True

        try:
            # Try opening the file with Zarr
            zarr.open_group(thing, mode='r')
            return True
        except Exception:
            # If an error occurs, it's likely not a Zarr file
            return False
        
    def open_trajectory(self):
        """opens the trajectory file using zarr library"""
        self._frame = -1
        if isinstance(self.filename, zarr.Group):
            self._file = self.filename
        else:
            self._file = zarr.open_group(self.filename,
                                         mode='r')
        # pulls first key out of 'particles'
        # allows for arbitrary name of group1 in 'particles'
        self._particle_group = self._file['particles'][
            list(self._file['particles'])[0]]
     
    @staticmethod
    def parse_n_atoms(filename):
        # NOTE: This may fail if the filename passed is already a zarr group
        with zarr.open_group(filename, 'r') as f:
            for group in f['particles/trajectory']:
                if group in ('position', 'velocity', 'force'):
                    n_atoms = f[f'particles/trajectory/{group}/value'].shape[1]
                    return n_atoms

            raise NoDataError("Could not construct minimal topology from the "
                            "Zarrtraj trajectory file, as it did not contain a "
                            "'position', 'velocity', or 'force' group. "
                            "You must include a topology file.")
    
    def close(self):
        """close reader"""
        self._file.store.close()
    
    def _reopen(self):
        """reopen trajectory"""
        self.close()
        self.open_trajectory()

    @property
    def n_frames(self):
        """number of frames in trajectory"""
        for name, value in self._has.items():
            if value:
                return self._particle_group[name]['value'].shape[0]
            
    def _read_frame(self, frame):
        """reads data from zarrtraj file and copies to current timestep"""
        try:
            for name, value in self._has.items():
                if value:
                    _ = self._particle_group[name]['step'][frame]
                    break
            else:
                raise NoDataError("Provide at least a position, velocity"
                                  " or force group in the zarrtraj file.")
        except (ValueError, IndexError):
            raise IOError from None

        self._frame = frame
        ts = self.ts
        particle_group = self._particle_group
        ts.frame = frame

        # fills data dictionary from 'observables' group
        # Note: dt is not read into data as it is not decided whether
        # Timestep should have a dt attribute (see Issue #2825)
        self._copy_to_data()

        # Sets frame box dimensions
        # Note: Zarrtraj files must contain 'box' group in each 'particles' group
        if "edges" in particle_group["box"]:
            edges = particle_group["box/edges/value"][frame, :]
            # A D-dimensional vector or a D × D matrix, depending on the
            # geometry of the box, of Float or Integer type. If edges is a
            # vector, it specifies the space diagonal of a cuboid-shaped box.
            # If edges is a matrix, the box is of triclinic shape with the edge
            # vectors given by the rows of the matrix.
            if edges.shape == (3,):
                ts.dimensions = [*edges, 90, 90, 90]
            else:
                ts.dimensions = core.triclinic_box(*edges)
        else:
            ts.dimensions = None

        # set the timestep positions, velocities, and forces with
        # current frame dataset
        if self._has['position']:
            self._read_dataset_into_ts('position', ts.positions)
        if self._has['velocity']:
            self._read_dataset_into_ts('velocity', ts.velocities)
        if self._has['force']:
            self._read_dataset_into_ts('force', ts.forces)

        # NOTE: Not sure about unit conversions yet
        #if self.convert_units:
        #    self._convert_units()

        return ts
    
    def _copy_to_data(self):
        """assigns values to keys in data dictionary"""

        # pulls 'time' and 'step' out of first available parent group
        for name, value in self._has.items():
            if value:
                if 'time' in self._particle_group[name]:
                    self.ts.time = self._particle_group[name][
                        'time'][self._frame]
                    break
        for name, value in self._has.items():
            if value:
                if 'step' in self._particle_group[name]:
                    self.ts.data['step'] = self._particle_group[name][
                        'step'][self._frame]
                    break

    def _read_dataset_into_ts(self, dataset, attribute):
        """reads position, velocity, or force dataset array at current frame
        into corresponding ts attribute"""

        n_atoms_now = self._particle_group[f'{dataset}/value'][
                                           self._frame].shape[0]
        if n_atoms_now != self.n_atoms:
            raise ValueError(f"Frame {self._frame} of the {dataset} dataset"
                             f" has {n_atoms_now} atoms but the initial frame"
                             " of either the postion, velocity, or force"
                             f" dataset had {self.n_atoms} atoms."
                             " MDAnalysis is unable to deal"
                             " with variable topology!")

        self._particle_group[f'{dataset}/value'].read_direct(
                             attribute, source_sel=np.s_[self._frame, :])
    
    def _read_next_timestep(self):
        """read next frame in trajectory"""
        return self._read_frame(self._frame + 1)
    
    @property
    def has_positions(self):
        """``True`` if 'position' group is in trajectory."""
        return self._has['position']

    @has_positions.setter
    def has_positions(self, value: bool):
        self._has['position'] = value

    @property
    def has_velocities(self):
        """``True`` if 'velocity' group is in trajectory."""
        return self._has['velocity']

    @has_velocities.setter
    def has_velocities(self, value: bool):
        self._has['velocity'] = value

    @property
    def has_forces(self):
        """``True`` if 'force' group is in trajectory."""
        return self._has['force']

    @has_forces.setter
    def has_forces(self, value: bool):
        self._has['force'] = value

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['_particle_group']
        return state

    def __setstate__(self, state):
        self.__dict__ = state
        self._particle_group = self._file['particles'][
                               list(self._file['particles'])[0]]
        self[self.ts.frame]

class ZarrTrajWriter(base.WriterBase):
    format = 'ZARRTRAJ'
    multiframe = True

    raise NotImplementedError("There is currently no writer for TNG files")
