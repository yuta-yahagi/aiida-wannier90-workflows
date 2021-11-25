# -*- coding: utf-8 -*-
"""Base class for Wannierisation workflow."""
import typing as ty
from copy import deepcopy
import numpy as np

from aiida import orm
from aiida.common import AttributeDict
from aiida.common.lang import type_check
from aiida.engine.processes import WorkChain, ToContext, if_, ProcessBuilder
from aiida.engine.processes import calcfunction
from aiida_pseudo.data.pseudo import UpfData
from aiida_quantumespresso.utils.mapping import prepare_process_inputs
from aiida_quantumespresso.calculations.pw import PwCalculation
from aiida_quantumespresso.workflows.pw.base import PwBaseWorkChain
from aiida_quantumespresso.workflows.pw.relax import PwRelaxWorkChain
from aiida_quantumespresso.calculations.projwfc import ProjwfcCalculation
from aiida_quantumespresso.common.types import ElectronicType, SpinType
from aiida_quantumespresso.workflows.protocols.utils import ProtocolMixin
from aiida_wannier90.calculations import Wannier90Calculation

from ..common.types import WannierProjectionType, WannierDisentanglementType, WannierFrozenType
from .base.wannier90 import Wannier90BaseWorkChain
from .base.pw2wannier90 import Pw2wannier90BaseWorkChain
from ..utils.kmesh import get_explicit_kpoints_from_distance, get_explicit_kpoints, create_kpoints_from_distance
from ..utils.scdm import fit_scdm_mu_sigma_aiida, get_energy_of_projectability
from ..utils.upf import get_number_of_projections, get_wannier_number_of_bands, _load_pseudo_metadata

__all__ = ['Wannier90WorkChain']


# pylint: disable=fixme,too-many-lines
class Wannier90WorkChain(ProtocolMixin, WorkChain):  # pylint: disable=too-many-public-methods
    """Workchain to obtain maximally localised Wannier functions (MLWF).

    Will run the following steps:
        relax(optional) --> scf --> nscf --> projwfc ->
        wannier90_postproc --> pw2wannier90 --> wannier90
    """

    @classmethod
    def define(cls, spec):
        """Define the process spec."""
        super().define(spec)

        spec.input('structure', valid_type=orm.StructureData, help='The input structure.')
        spec.input(
            'clean_workdir',
            valid_type=orm.Bool,
            required=False,
            default=lambda: orm.Bool(False),
            help=('If `True`, work directories of all called calculation will be cleaned '
                  'at the end of execution.')
        )
        spec.input(
            'relative_dis_windows',
            valid_type=orm.Bool,
            required=False,
            default=lambda: orm.Bool(False),
            help=(
                'If True the dis_froz/win_min/max will be shifted by Fermi enerngy (for metals) '
                'or minimum of lowest-unoccupied bands (for insulators). '
                'False is the default behaviour of wannier90.'
            )
        )
        spec.input(
            'scdm_sigma_factor',
            valid_type=orm.Float,
            required=False,
            default=lambda: orm.Float(3.0),
            help='For SCDM projection.'
        )
        spec.input(
            'auto_froz_max',
            valid_type=orm.Bool,
            required=False,
            default=lambda: orm.Bool(False),
            help=(
                'If True use the energy corresponding to projectability = 0.9 '
                'as `dis_froz_max` for wannier90 projectability disentanglement.'
            )
        )
        spec.input(
            'auto_froz_max_threshold',
            valid_type=orm.Float,
            required=False,
            default=lambda: orm.Float(0.9),
            help='Threshold for auto_froz_max.'
        )
        spec.expose_inputs(
            PwRelaxWorkChain,
            namespace='relax',
            exclude=('clean_workdir', 'structure'),
            namespace_options={
                'required': False,
                'populate_defaults': False,
                'help':
                ('Inputs for the `PwRelaxWorkChain`, if not specified at all, '
                 'the relaxation step is skipped.')
            }
        )
        spec.expose_inputs(
            PwBaseWorkChain,
            namespace='scf',
            exclude=('clean_workdir', 'pw.structure'),
            namespace_options={
                'required': False,
                'populate_defaults': False,
                'help': 'Inputs for the `PwBaseWorkChain` for the SCF calculation.'
            }
        )
        spec.expose_inputs(
            PwBaseWorkChain,
            namespace='nscf',
            exclude=('clean_workdir', 'pw.structure'),
            namespace_options={
                'required': False,
                'populate_defaults': False,
                'help': 'Inputs for the `PwBaseWorkChain` for the NSCF calculation.'
            }
        )
        spec.inputs['nscf']['pw'].validator = PwCalculation.validate_inputs_base
        spec.expose_inputs(
            ProjwfcCalculation,
            namespace='projwfc',
            exclude=('parent_folder',),
            namespace_options={
                'required': False,
                'populate_defaults': False,
                'help': 'Inputs for the `ProjwfcCalculation`.'
            }
        )
        spec.expose_inputs(
            Pw2wannier90BaseWorkChain,
            namespace='pw2wannier90',
            exclude=('clean_workdir', 'pw2wannier90.parent_folder', 'pw2wannier90.nnkp_file'),
            namespace_options={'help': 'Inputs for the `Pw2wannier90BaseWorkChain`.'}
        )
        spec.expose_inputs(
            Wannier90BaseWorkChain,
            namespace='wannier90',
            exclude=('clean_workdir', 'wannier90.structure'),
            namespace_options={'help': 'Inputs for the `Wannier90BaseWorkChain`.'}
        )
        spec.inputs.validator = cls.validate_inputs

        spec.outline(
            cls.setup,
            if_(cls.should_run_relax)(
                cls.run_relax,
                cls.inspect_relax,
            ),
            if_(cls.should_run_scf)(
                cls.run_scf,
                cls.inspect_scf,
            ),
            if_(cls.should_run_nscf)(
                cls.run_nscf,
                cls.inspect_nscf,
            ),
            if_(cls.should_run_projwfc)(
                cls.run_projwfc,
                cls.inspect_projwfc,
            ),
            cls.run_wannier90_pp,
            cls.inspect_wannier90_pp,
            cls.run_pw2wannier90,
            cls.inspect_pw2wannier90,
            cls.run_wannier90,
            cls.inspect_wannier90,
            cls.results,
        )

        spec.expose_outputs(PwRelaxWorkChain, namespace='relax', namespace_options={'required': False})
        spec.expose_outputs(PwBaseWorkChain, namespace='scf', namespace_options={'required': False})
        spec.expose_outputs(PwBaseWorkChain, namespace='nscf', namespace_options={'required': False})
        spec.expose_outputs(ProjwfcCalculation, namespace='projwfc', namespace_options={'required': False})
        spec.expose_outputs(Pw2wannier90BaseWorkChain, namespace='pw2wannier90')
        spec.expose_outputs(Wannier90BaseWorkChain, namespace='wannier90_pp')
        spec.expose_outputs(Wannier90BaseWorkChain, namespace='wannier90')

        spec.exit_code(410, 'ERROR_SUB_PROCESS_FAILED_RELAX', message='the PwRelaxWorkChain sub process failed')
        spec.exit_code(420, 'ERROR_SUB_PROCESS_FAILED_SCF', message='the scf PwBasexWorkChain sub process failed')
        spec.exit_code(430, 'ERROR_SUB_PROCESS_FAILED_NSCF', message='the nscf PwBasexWorkChain sub process failed')
        spec.exit_code(
            431,
            'ERROR_NO_FERMI_FOR_RELATIVE_DIS_WINDOWS',
            message='`relative_dis_windows` is True but no Fermi energy parsed from scf/nscf outputs'
        )
        spec.exit_code(440, 'ERROR_SUB_PROCESS_FAILED_PROJWFC', message='the ProjwfcCalculation sub process failed')
        spec.exit_code(
            450,
            'ERROR_SUB_PROCESS_FAILED_WANNIER90PP',
            message='the postproc Wannier90BaseWorkChain sub process failed'
        )
        spec.exit_code(
            460, 'ERROR_SUB_PROCESS_FAILED_PW2WANNIER90', message='the Pw2wannier90BaseWorkChain sub process failed'
        )
        spec.exit_code(
            470, 'ERROR_SUB_PROCESS_FAILED_WANNIER90', message='the Wannier90BaseWorkChain sub process failed'
        )

    @staticmethod
    def validate_inputs(inputs, ctx=None):  # pylint: disable=unused-argument
        """Validate the inputs of the entire input namespace."""
        # If no scf inputs, the nscf must have a `parent_folder`
        if 'scf' not in inputs:
            nscf_inputs = AttributeDict(inputs['nscf'])
            if 'parent_folder' not in nscf_inputs['pw']:
                return 'If skipping scf step, nscf inputs must have a `parent_folder`'

        wannier_inputs = AttributeDict(inputs['wannier90']['wannier90'])
        wannier_parameters = wannier_inputs.parameters.get_dict()

        # Check bands_plot and kpoint_path, explicit_kpoint_path
        bands_plot = wannier_parameters.get('bands_plot', False)
        if bands_plot:
            kpoint_path = wannier_inputs.get('kpoint_path', None)
            explicit_kpoint_path = wannier_inputs.get('explicit_kpoint_path', None)
            if kpoint_path is None and explicit_kpoint_path is None:
                return 'bands_plot is True but no kpoint_path or explicit_kpoint_path provided'

        # Cannot specify both `auto_froz_max` and `scdm_proj`
        pw2wannier_inputs = AttributeDict(inputs['pw2wannier90']['pw2wannier90'])
        pw2wannier_parameters = pw2wannier_inputs.parameters.get_dict()
        if inputs.get('auto_froz_max', False) and pw2wannier_parameters['inputpp'].get('scdm_proj', False):
            return '`auto_froz_max` is incompatible with SCDM'

    def setup(self):
        """Define the current structure in the context to be the input structure."""
        self.ctx.current_structure = self.inputs.structure

        if not self.should_run_scf():
            self.ctx.current_folder = self.inputs['nscf']['pw']['parent_folder']

    def should_run_relax(self):
        """If the 'relax' input namespace was specified, will relax the input structure."""
        return 'relax' in self.inputs

    def run_relax(self):
        """Run the `PwRelaxWorkChain` to relax the structure."""
        inputs = AttributeDict(self.exposed_inputs(PwRelaxWorkChain, namespace='relax'))
        inputs.structure = self.ctx.current_structure
        inputs.metadata.call_link_label = 'relax'

        inputs = prepare_process_inputs(PwRelaxWorkChain, inputs)
        running = self.submit(PwRelaxWorkChain, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}>')

        return ToContext(workchain_relax=running)

    def inspect_relax(self):
        """Verify that the `PwRelaxWorkChain` successfully finished."""
        workchain = self.ctx.workchain_relax

        if not workchain.is_finished_ok:
            self.report(f'{workchain.process_label} failed with exit status {workchain.exit_status}')
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_RELAX

        self.ctx.current_structure = workchain.outputs.output_structure

    def should_run_scf(self):
        """If the 'scf' input namespace was specified, run the scf workchain."""
        return 'scf' in self.inputs

    def run_scf(self):
        """Run the `PwBaseWorkChain` in scf mode on the current structure."""
        inputs = AttributeDict(self.exposed_inputs(PwBaseWorkChain, namespace='scf'))
        inputs.pw.structure = self.ctx.current_structure
        inputs.metadata.call_link_label = 'scf'

        inputs = prepare_process_inputs(PwBaseWorkChain, inputs)
        running = self.submit(PwBaseWorkChain, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}> in scf mode')

        return ToContext(workchain_scf=running)

    def inspect_scf(self):
        """Verify that the `PwBaseWorkChain` for the scf run successfully finished."""
        workchain = self.ctx.workchain_scf

        if not workchain.is_finished_ok:
            self.report(f'scf {workchain.process_label} failed with exit status {workchain.exit_status}')
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_SCF

        self.ctx.current_folder = workchain.outputs.remote_folder

    def should_run_nscf(self):
        """If the `nscf` input namespace was specified, run the nscf workchain."""
        return 'nscf' in self.inputs

    def run_nscf(self):
        """Run the PwBaseWorkChain in nscf mode."""
        inputs = AttributeDict(self.exposed_inputs(PwBaseWorkChain, namespace='nscf'))
        inputs.pw.structure = self.ctx.current_structure
        inputs.pw.parent_folder = self.ctx.current_folder
        inputs.metadata.call_link_label = 'nscf'

        inputs = prepare_process_inputs(PwBaseWorkChain, inputs)
        running = self.submit(PwBaseWorkChain, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}> in nscf mode')

        return ToContext(workchain_nscf=running)

    def inspect_nscf(self):
        """Verify that the `PwBaseWorkChain` for the nscf run successfully finished."""
        workchain = self.ctx.workchain_nscf

        if not workchain.is_finished_ok:
            self.report(f'nscf {workchain.process_label} failed with exit status {workchain.exit_status}')
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_NSCF

        self.ctx.current_folder = workchain.outputs.remote_folder

    def should_run_projwfc(self):
        """If the 'projwfc' input namespace was specified, run the projwfc calculation.

        May be used by SCDM.
        """
        return 'projwfc' in self.inputs

    def run_projwfc(self):
        """Projwfc step."""
        inputs = AttributeDict(self.exposed_inputs(ProjwfcCalculation, namespace='projwfc'))
        inputs.parent_folder = self.ctx.current_folder
        inputs.metadata.call_link_label = 'projwfc'

        inputs = prepare_process_inputs(ProjwfcCalculation, inputs)
        running = self.submit(ProjwfcCalculation, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}>')

        return ToContext(calc_projwfc=running)

    def inspect_projwfc(self):
        """Verify that the `ProjwfcCalculation` for the projwfc run successfully finished."""
        calculation = self.ctx.calc_projwfc

        if not calculation.is_finished_ok:
            self.report(f'{calculation.process_label} failed with exit status {calculation.exit_status}')
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_PROJWFC

    def prepare_wannier90_inputs(self):  # pylint: disable=too-many-statements
        """Construct the inputs of wannier90 calculation.

        This is different from the classmethod `get_wannier90_inputs`, which statically generates
        inputs and is used by the `get_builder_from_protocol`.
        Here this method will be called by the workchain in runtime, so it can dynamically
        add/modify inputs based on outputs of previous calculations, e.g. adding Fermi energy, etc.
        Moreover, this allows overriding the method in derived classes to further modify the inputs.
        """
        from aiida_wannier90_workflows.utils.bandsdist import remove_exclude_bands

        base_inputs = AttributeDict(self.exposed_inputs(Wannier90BaseWorkChain, namespace='wannier90'))
        inputs = base_inputs['wannier90']
        inputs.structure = self.ctx.current_structure
        parameters = inputs.parameters.get_dict()

        fermi_energy = None
        if 'workchain_scf' in self.ctx:
            scf_output_parameters = self.ctx.workchain_scf.outputs.output_parameters
            fermi_energy = get_fermi_energy(scf_output_parameters)
        elif 'workchain_nscf' in self.ctx:
            fermi_energy = get_scf_fermi_energy(self.ctx.workchain_nscf)

        # Add Fermi energy
        if fermi_energy:
            parameters['fermi_energy'] = fermi_energy

        if self.inputs['relative_dis_windows']:
            # Need fermi energy to shift the windows
            if fermi_energy is None:
                raise ValueError('relative_dis_windows = True but cannot retrieve Fermi energy from scf or nscf output')

            # Check the system is metal or insulator.
            # For metal, we shift the four paramters by Fermi energy.
            # For insulator, we shift them by the minimum of LUMO.
            if 'workchain_scf' in self.ctx:
                output_band = self.ctx.workchain_scf.outputs.output_band
            elif 'workchain_nscf' in self.ctx:
                output_band = self.ctx.workchain_nscf.outputs.output_band
            else:
                raise ValueError('No output scf or nscf bands, cannot calculate bandgap')
            homo, lumo = get_homo_lumo(output_band.get_bands(), fermi_energy)
            bandgap = lumo - homo
            if bandgap > 1e-3:
                shift_energy = lumo
            else:
                shift_energy = fermi_energy

            keys = ['dis_froz_min', 'dis_froz_max', 'dis_win_min', 'dis_win_max']
            for k in keys:
                if k in parameters:
                    parameters[k] += shift_energy

        # Auto set dis_froz_max
        if 'auto_froz_max' in self.inputs and self.inputs.auto_froz_max:
            bands = self.ctx.calc_projwfc.outputs.bands
            projections = self.ctx.calc_projwfc.outputs.projections
            args = {'bands': bands, 'projections': projections}
            if 'auto_froz_max_threshold' in self.inputs:
                args['thresholds'] = self.inputs.auto_froz_max_threshold.value
            dis_froz_max = get_energy_of_projectability(**args)
            parameters['dis_froz_max'] = dis_froz_max

        # Prevent error:
        #   dis_windows: More states in the frozen window than target WFs
        if 'dis_froz_max' in parameters:
            if 'workchain_nscf' in self.ctx:
                bands = self.ctx.workchain_nscf.outputs.output_band
            else:
                bands = self.ctx.workchain_scf.outputs.output_band
            bands = bands.get_bands()
            if parameters.get('exclude_bands', None):
                # Index of parameters['exclude_bands'] starts from 1,
                # I need to change it to 0-based
                exclude_bands = [i - 1 for i in parameters['exclude_bands']]
                bands = remove_exclude_bands(bands=bands, exclude_bands=exclude_bands)
            highest_band = bands[:, parameters['num_wann'] - 1]
            # There must be more than 1 available bands for disentanglement,
            # this sets the upper limit of dis_froz_max.
            max_froz_energy = np.min(highest_band)
            # I subtract a small value for safety
            max_froz_energy -= 1e-4
            # dis_froz_max should be smaller than this max_froz_energy
            # to allow doing disentanglement
            dis_froz_max = min(max_froz_energy, parameters['dis_froz_max'])
            parameters['dis_froz_max'] = dis_froz_max

        inputs.parameters = orm.Dict(dict=parameters)

        base_inputs['wannier90'] = inputs

        return base_inputs

    def run_wannier90_pp(self):
        """Wannier90 post processing step."""
        base_inputs = self.prepare_wannier90_inputs()
        inputs = base_inputs['wannier90']

        # add postproc
        if 'settings' in inputs:
            settings = inputs['settings'].get_dict()
        else:
            settings = {}
        settings['postproc_setup'] = True
        inputs['settings'] = settings

        # I should not stash files in postproc, otherwise there is a RemoteStashFolderData in outputs
        inputs['metadata']['options'].pop('stash', None)

        base_inputs['wannier90'] = inputs
        base_inputs['metadata'] = {'call_link_label': 'wannier90_pp'}
        base_inputs['clean_workdir'] = orm.Bool(False)
        inputs = prepare_process_inputs(Wannier90BaseWorkChain, base_inputs)

        running = self.submit(Wannier90BaseWorkChain, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}> in postproc mode')

        return ToContext(workchain_wannier90_pp=running)

    def inspect_wannier90_pp(self):
        """Verify that the `Wannier90Calculation` for the wannier90 run successfully finished."""
        workchain = self.ctx.workchain_wannier90_pp

        if not workchain.is_finished_ok:
            self.report(f'wannier90 postproc {workchain.process_label} failed with exit status {workchain.exit_status}')
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_WANNIER90PP

    def prepare_pw2wannier90_inputs(self):
        """Construct the inputs of Pw2wannier90BaseWorkChain.

        This is different from the classmethod `get_pw2wannier90_inputs`, which statically generates
        inputs and is used by the `get_builder_from_protocol`.
        Here this method will be called by the workchain in runtime, so it can dynamically
        add/modify inputs based on outputs of previous calculations,
        e.g. calculating scdm_mu/sigma from projectability, etc.
        Moreover, this method can be overridden in derived classes.
        """
        base_inputs = AttributeDict(self.exposed_inputs(Pw2wannier90BaseWorkChain, namespace='pw2wannier90'))
        inputs = base_inputs['pw2wannier90']

        inputs['parent_folder'] = self.ctx.current_folder
        inputs['nnkp_file'] = self.ctx.workchain_wannier90_pp.outputs.nnkp_file

        inputpp = inputs.parameters.get_dict().get('inputpp', {})
        scdm_proj = inputpp.get('scdm_proj', False)
        scdm_entanglement = inputpp.get('scdm_entanglement', None)
        scdm_mu = inputpp.get('scdm_mu', None)
        scdm_sigma = inputpp.get('scdm_sigma', None)

        calc_scdm_params = scdm_proj and scdm_entanglement == 'erfc'
        calc_scdm_params = calc_scdm_params and (scdm_mu is None or scdm_sigma is None)

        if scdm_entanglement == 'gaussian':
            if scdm_mu is None or scdm_sigma is None:
                raise ValueError('scdm_entanglement = gaussian but scdm_mu or scdm_sigma is empty.')

        if calc_scdm_params:
            if 'calc_projwfc' not in self.ctx:
                raise ValueError('Needs to run projwfc before auto-generating scdm_mu/sigma')
            try:
                args = {
                    'parameters': inputs.parameters,
                    'bands': self.ctx.calc_projwfc.outputs.bands,
                    'projections': self.ctx.calc_projwfc.outputs.projections,
                    'sigma_factor': self.inputs.scdm_sigma_factor,
                    'metadata': {
                        'call_link_label': 'update_scdm_mu_sigma'
                    }
                }
                inputs.parameters = update_scdm_mu_sigma(**args)  # pylint: disable=unexpected-keyword-arg
            except Exception as exc:
                raise ValueError(f'update_scdm_mu_sigma failed! {exc.args}') from exc

        base_inputs['pw2wannier90'] = inputs

        return base_inputs

    def run_pw2wannier90(self):
        """Run the pw2wannier90 step."""
        inputs = self.prepare_pw2wannier90_inputs()
        inputs.metadata.call_link_label = 'pw2wannier90'

        inputs = prepare_process_inputs(Pw2wannier90BaseWorkChain, inputs)
        running = self.submit(Pw2wannier90BaseWorkChain, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}>')

        return ToContext(workchain_pw2wannier90=running)

    def inspect_pw2wannier90(self):
        """Verify that the Pw2wannier90BaseWorkChain for the pw2wannier90 run successfully finished."""
        workchain = self.ctx.workchain_pw2wannier90

        if not workchain.is_finished_ok:
            self.report(f'{workchain.process_label} failed with exit status {workchain.exit_status}')
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_PW2WANNIER90

        self.ctx.current_folder = workchain.outputs.remote_folder

    def run_wannier90(self):
        """Wannier90 step for MLWF."""
        from aiida_wannier90_workflows.utils.node import get_last_calcjob

        base_inputs = AttributeDict(self.exposed_inputs(Wannier90BaseWorkChain, namespace='wannier90'))
        inputs = base_inputs['wannier90']

        # I should stash files, which was removed from metadata in the postproc step
        stash = None
        if 'stash' in inputs['metadata']['options']:
            stash = deepcopy(inputs['metadata']['options']['stash'])

        # Use the Wannier90BaseWorkChain-corrected parameters
        last_calc = get_last_calcjob(self.ctx.workchain_wannier90_pp)
        # copy postproc inputs, especially the `kmesh_tol` might have been corrected
        for key in last_calc.inputs:
            inputs[key] = last_calc.inputs[key]

        inputs['remote_input_folder'] = self.ctx.current_folder

        if 'settings' in inputs:
            settings = inputs.settings.get_dict()
        else:
            settings = {}
        settings['postproc_setup'] = False

        inputs.settings = settings

        # Restore stash files
        if stash:
            options = deepcopy(inputs['metadata']['options'])
            options['stash'] = stash
            inputs['metadata']['options'] = options

        base_inputs['wannier90'] = inputs
        base_inputs['metadata'] = {'call_link_label': 'wannier90'}
        base_inputs['clean_workdir'] = orm.Bool(False)
        inputs = prepare_process_inputs(Wannier90BaseWorkChain, base_inputs)

        running = self.submit(Wannier90BaseWorkChain, **inputs)
        self.report(f'launching {running.process_label}<{running.pk}>')

        return ToContext(workchain_wannier90=running)

    def inspect_wannier90(self):
        """Verify that the `Wannier90BaseWorkChain` for the wannier90 run successfully finished."""
        workchain = self.ctx.workchain_wannier90

        if not workchain.is_finished_ok:
            self.report(f'{workchain.process_label} failed with exit status {workchain.exit_status}')
            return self.exit_codes.ERROR_SUB_PROCESS_FAILED_WANNIER90

        self.ctx.current_folder = workchain.outputs.remote_folder

    def results(self):
        """Attach the desired output nodes directly as outputs of the workchain."""
        if 'workchain_relax' in self.ctx:
            self.out_many(self.exposed_outputs(self.ctx.workchain_relax, PwRelaxWorkChain, namespace='relax'))

        if 'workchain_scf' in self.ctx:
            self.out_many(self.exposed_outputs(self.ctx.workchain_scf, PwBaseWorkChain, namespace='scf'))

        if 'workchain_nscf' in self.ctx:
            self.out_many(self.exposed_outputs(self.ctx.workchain_nscf, PwBaseWorkChain, namespace='nscf'))

        if 'calc_projwfc' in self.ctx:
            self.out_many(self.exposed_outputs(self.ctx.calc_projwfc, ProjwfcCalculation, namespace='projwfc'))

        self.out_many(
            self.exposed_outputs(self.ctx.workchain_pw2wannier90, Pw2wannier90BaseWorkChain, namespace='pw2wannier90')
        )
        self.out_many(
            self.exposed_outputs(self.ctx.workchain_wannier90_pp, Wannier90BaseWorkChain, namespace='wannier90_pp')
        )
        self.out_many(self.exposed_outputs(self.ctx.workchain_wannier90, Wannier90BaseWorkChain, namespace='wannier90'))

        # not necessary but it is good to do some sanity checks:
        # 1. the calculated number of projections is consistent with QE projwfc.x
        from ..utils.upf import get_number_of_electrons
        if 'scf' in self.inputs:
            pseudos = self.inputs['scf']['pw']['pseudos']
        else:
            pseudos = self.inputs['nscf']['pw']['pseudos']
        args = {
            'structure': self.ctx.current_structure,
            # the type of `self.inputs['scf']['pw']['pseudos']` is plumpy.utils.AttributesFrozendict,
            # we need to convert it to dict, otherwise get_number_of_projections will fail.
            'pseudos': dict(pseudos)
        }
        if 'calc_projwfc' in self.ctx:
            num_proj = len(self.ctx.calc_projwfc.outputs['projections'].get_orbitals())
            params = self.ctx.workchain_wannier90.inputs['wannier90']['parameters'].get_dict()
            spin_orbit_coupling = params.get('spinors', False)
            number_of_projections = get_number_of_projections(**args, spin_orbit_coupling=spin_orbit_coupling)
            if number_of_projections != num_proj:
                raise ValueError(f'number of projections {number_of_projections} != projwfc.x output {num_proj}')
        # 2. the number of electrons is consistent with QE output
        if 'workchain_scf' in self.ctx:
            num_elec = self.ctx.workchain_scf.outputs['output_parameters']['number_of_electrons']
        else:
            num_elec = self.ctx.workchain_nscf.outputs['output_parameters']['number_of_electrons']
        number_of_electrons = get_number_of_electrons(**args)
        if number_of_electrons != num_elec:
            raise ValueError(f'number of electrons {number_of_electrons} != QE output {num_elec}')

        self.report(f'{self.get_name()} successfully completed')

    def on_terminated(self):
        """Clean the working directories of all child calculations if `clean_workdir=True` in the inputs."""
        super().on_terminated()

        if not self.inputs.clean_workdir:
            self.report('remote folders will not be cleaned')
            return

        cleaned_calcs = []

        for called_descendant in self.node.called_descendants:
            if isinstance(called_descendant, orm.CalcJobNode):
                try:
                    called_descendant.outputs.remote_folder._clean()  # pylint: disable=protected-access
                    cleaned_calcs.append(called_descendant.pk)
                except (IOError, OSError, KeyError):
                    pass

        if cleaned_calcs:
            self.report(f"cleaned remote folders of calculations: {' '.join(map(str, cleaned_calcs))}")

    @classmethod
    def get_protocol_filepath(cls):
        """Return ``pathlib.Path`` to the ``.yaml`` file that defines the protocols."""
        from importlib_resources import files
        from . import protocols as wannier_protocols
        return files(wannier_protocols) / 'wannier.yaml'

    @classmethod
    def get_relax_inputs(cls, code, kpoints_distance, pseudo_family=None, **kwargs):
        """Statically generate the inputs for `PwRelaxWorkChain`."""
        overrides = {'clean_workdir': orm.Bool(False), 'base': {'kpoints_distance': kpoints_distance}}
        if pseudo_family is not None:
            overrides['pseudo_family'] = pseudo_family

        # PwBaseWorkChain.get_builder_from_protocol() does not support SOC, I have to
        # pretend that I am doing an non-SOC calculation and add SOC parameters later.
        spin_type = kwargs.get('spin_type', SpinType.NONE)
        filtered_kwargs = deepcopy(kwargs)
        if spin_type == SpinType.SPIN_ORBIT:
            filtered_kwargs['spin_type'] = SpinType.NONE
            if pseudo_family is None:
                raise ValueError('`pseudo_family` should be explicitly set for SOC')

        builder = PwRelaxWorkChain.get_builder_from_protocol(code=code, overrides=overrides, **filtered_kwargs)

        parameters = builder.base['pw']['parameters'].get_dict()
        if spin_type == SpinType.NON_COLLINEAR:
            parameters['SYSTEM']['noncolin'] = True
        if spin_type == SpinType.SPIN_ORBIT:
            parameters['SYSTEM']['noncolin'] = True
            parameters['SYSTEM']['lspinorb'] = True
        builder.base['pw']['parameters'] = orm.Dict(dict=parameters)

        excluded_inputs = ['clean_workdir', 'structure']
        inputs = {}
        for key in builder:
            if key in excluded_inputs:
                continue
            inputs[key] = builder[key]

        return inputs

    @classmethod
    def get_scf_inputs(cls, code, kpoints_distance, pseudo_family=None, **kwargs):
        """Statically generate the inputs for `PwBaseWorkChain`."""
        overrides = {'clean_workdir': orm.Bool(False), 'kpoints_distance': kpoints_distance}
        if pseudo_family is not None:
            overrides['pseudo_family'] = pseudo_family

        # PwBaseWorkChain.get_builder_from_protocol() does not support SOC, I have to
        # pretend that I am doing an non-SOC calculation and add SOC parameters later.
        spin_type = kwargs.get('spin_type', SpinType.NONE)
        filtered_kwargs = deepcopy(kwargs)
        if spin_type == SpinType.SPIN_ORBIT:
            filtered_kwargs['spin_type'] = SpinType.NONE
            if pseudo_family is None:
                raise ValueError('`pseudo_family` should be explicitly set for SOC')

        builder = PwBaseWorkChain.get_builder_from_protocol(code=code, overrides=overrides, **filtered_kwargs)

        parameters = builder['pw']['parameters'].get_dict()
        if spin_type == SpinType.NON_COLLINEAR:
            parameters['SYSTEM']['noncolin'] = True
        if spin_type == SpinType.SPIN_ORBIT:
            parameters['SYSTEM']['noncolin'] = True
            parameters['SYSTEM']['lspinorb'] = True
        builder['pw']['parameters'] = orm.Dict(dict=parameters)

        # Currently only support magnetic with SOC
        # for magnetic w/o SOC, needs 2 separate wannier90 calculations for spin up and down.
        # if self.inputs.spin_polarized and self.inputs.spin_orbit_coupling:
        #     # Magnetization from Kittel, unit: Bohr magneton
        #     magnetizations = {'Fe': 2.22, 'Co': 1.72, 'Ni': 0.606}
        #     from aiida_wannier90_workflows.utils.upf import get_number_of_electrons_from_upf
        #     for i, kind in enumerate(self.inputs.structure.kinds):
        #         if kind.name in magnetizations:
        #             zvalence = get_number_of_electrons_from_upf(
        #                 self.ctx.pseudos[kind.name]
        #             )
        #             spin_polarization = magnetizations[kind.name] / zvalence
        #             pw_parameters['SYSTEM'][f"starting_magnetization({i+1})"
        #                                     ] = spin_polarization

        excluded_inputs = ['clean_workdir']
        inputs = {}
        for key in builder:
            if key in excluded_inputs:
                continue
            inputs[key] = builder[key]
        # structure is in the pw namespace, I need to pop it
        inputs['pw'].pop('structure', None)

        return inputs

    @classmethod
    def get_nscf_inputs(cls, code, kpoints_distance, nbands_factor, pseudo_family=None, **kwargs):
        """Statically generate the inputs for `PwBaseWorkChain`."""

        inputs = cls.get_scf_inputs(code, kpoints_distance, pseudo_family, **kwargs)

        only_valence = kwargs.get('electronic_type', None) == ElectronicType.INSULATOR
        spin_polarized = kwargs.get('spin_type', SpinType.NONE) == SpinType.COLLINEAR
        spin_orbit_coupling = kwargs.get('spin_type', SpinType.NONE) == SpinType.SPIN_ORBIT

        nbnd = get_wannier_number_of_bands(
            structure=kwargs['structure'],
            pseudos=inputs['pw']['pseudos'],
            factor=nbands_factor,
            only_valence=only_valence,
            spin_polarized=spin_polarized,
            spin_orbit_coupling=spin_orbit_coupling
        )

        parameters = inputs['pw']['parameters'].get_dict()
        parameters['SYSTEM']['nbnd'] = nbnd

        parameters['SYSTEM']['nosym'] = True
        parameters['SYSTEM']['noinv'] = True

        parameters['CONTROL']['calculation'] = 'nscf'
        parameters['CONTROL']['restart_mode'] = 'from_scratch'
        parameters['ELECTRONS']['startingpot'] = 'file'
        # I switched to the QE default `david` diagonalization, since now
        # aiida-qe has an error handler to switch to `cg` if `david` fails.
        # See https://github.com/aiidateam/aiida-quantumespresso/pull/744
        # parameters['ELECTRONS']['diagonalization'] = 'david'
        parameters['ELECTRONS']['diago_full_acc'] = True

        inputs['pw']['parameters'] = orm.Dict(dict=parameters)

        excluded_inputs = ['clean_workdir', 'structure']
        for k in excluded_inputs:
            inputs.pop(k, None)
        # structure is in the pw namespace, I need to pop it
        inputs['pw'].pop('structure', None)

        # use explicit list of kpoints, since auto generated kpoints
        # maybe different between QE & Wannier90. Here we explicitly
        # generate a list of kpoint to avoid discrepencies between
        # QE's & Wannier90's automatically generated kpoints.
        kpoints = get_explicit_kpoints_from_distance(kwargs['structure'], kpoints_distance)
        inputs.pop('kpoints_distance', None)
        inputs['kpoints'] = kpoints

        return inputs

    @classmethod
    def get_projwfc_inputs(cls, code, **kwargs):  # pylint: disable=unused-argument
        """Statically generate the inputs for `ProjwfcCalculation`."""
        parameters = orm.Dict(dict={'PROJWFC': {'DeltaE': 0.2}})

        inputs = {
            'code': code,
            'parameters': parameters,
            'metadata': {
                'options': {
                    'resources': {
                        'num_machines': 1
                    }
                },
            }
        }

        return inputs

    @classmethod
    def get_pw2wannier90_inputs(cls, code, *, projection_type, exclude_pswfcs=None, write_unk=False, **kwargs):
        """Statically generate the inputs for `Pw2wannier90Calculation`.

        Note the `scdm_mu` and `scdm_sigma` are generated in runtime by parsing the output of
        `ProjwfcCalculation`, so they are left empty here.
        """
        parameters = {
            # Default are True
            # 'write_mmn': True,
            # 'write_amn': True,
        }
        # write UNK files (to plot WFs)
        if write_unk:
            parameters['write_unk'] = True

        if projection_type == WannierProjectionType.SCDM:
            parameters['scdm_proj'] = True

            if kwargs['electronic_type'] == ElectronicType.INSULATOR:
                parameters['scdm_entanglement'] = 'isolated'
            else:
                parameters['scdm_entanglement'] = 'erfc'
                # scdm_mu, scdm_sigma will be set after projwfc run
        elif projection_type in [
            WannierProjectionType.ATOMIC_PROJECTORS_QE,
            WannierProjectionType.ATOMIC_PROJECTORS_OPENMX,
        ]:
            parameters['atom_proj'] = True
            if exclude_pswfcs is not None and len(exclude_pswfcs) > 0:
                parameters['atom_proj_exclude'] = list(exclude_pswfcs)
            if projection_type == WannierProjectionType.ATOMIC_PROJECTORS_OPENMX:
                # TODO
                parameters['atom_proj_ext'] = True
                parameters['atom_proj_dir'] = './'

        parameters = orm.Dict(dict={'inputpp': parameters})
        inputs = {
            'code': code,
            'parameters': parameters,
            'metadata': {
                'options': {
                    'resources': {
                        'num_machines': 1
                    }
                },
            }
        }
        return inputs

    @classmethod
    def get_wannier90_inputs( # pylint: disable=too-many-statements
        cls,
        code,
        *,
        projection_type,
        disentanglement_type,
        frozen_type,
        kpoints_distance,
        nbands,
        pseudos,
        maximal_localisation=None,
        exclude_semicores=True,
        plot_wannier_functions=False,
        retrieve_hamiltonian=False,
        retrieve_matrices=False,
        **kwargs
    ):
        """Statically generate the inputs for `Wannier90Calculation`."""
        inputs = {
            'code': code,
            'settings': {},
        }
        parameters = {
            # default is True
            # 'use_ws_distance': True,
        }

        structure = kwargs['structure']

        # Set num_bands, num_wann, also take care of semicore states
        parameters['num_bands'] = nbands
        spin_orbit_coupling = kwargs['spin_type'] == SpinType.SPIN_ORBIT
        num_projs = get_number_of_projections(structure, pseudos, spin_orbit_coupling=spin_orbit_coupling)

        # TODO check nospin, spin, soc
        if kwargs['electronic_type'] == ElectronicType.INSULATOR:
            num_wann = parameters['num_bands']
        else:
            num_wann = num_projs

        if exclude_semicores:
            pseudo_orbitals = get_pseudo_orbitals(pseudos)
            # TODO now only consider SSSP
            semicore_list = get_semicore_list(structure, pseudo_orbitals)
            num_excludes = len(semicore_list)
            # TODO I assume all the semicore bands are the lowest
            exclude_pswfcs = range(1, num_excludes + 1)
            if num_excludes != 0:
                parameters['exclude_bands'] = exclude_pswfcs
                num_wann -= num_excludes
                parameters['num_bands'] -= num_excludes

        if num_wann <= 0:
            raise ValueError(f'Wrong num_wann {num_wann}')
        parameters['num_wann'] = num_wann

        # Set projections
        if projection_type in [
            WannierProjectionType.SCDM, WannierProjectionType.ATOMIC_PROJECTORS_QE,
            WannierProjectionType.ATOMIC_PROJECTORS_OPENMX
        ]:
            parameters['auto_projections'] = True
        elif projection_type == WannierProjectionType.ANALYTIC:
            pseudo_orbitals = get_pseudo_orbitals(pseudos)
            projections = []
            # TODO
            # self.ctx.wannier_projections = orm.List(
            #     list=get_projections(**args)
            # )
            for site in structure.sites:
                for orb in pseudo_orbitals[site.kind_name]['pswfcs']:
                    if exclude_semicores:
                        if orb in pseudo_orbitals[site.kind_name]['semicores']:
                            continue
                    projections.append(f'{site.kind_name}:{orb[-1].lower()}')
            inputs['projections'] = projections
        elif projection_type == WannierProjectionType.RANDOM:
            inputs['settings'].update({'random_projections': True})
        else:
            raise ValueError(f'Unrecognized projection type {projection_type}')

        if kwargs['spin_type'] in [SpinType.NON_COLLINEAR, SpinType.SPIN_ORBIT]:
            parameters['spinors'] = True

        if plot_wannier_functions:
            parameters['wannier_plot'] = True

        default_num_iter = 4000
        num_atoms = len(structure.sites)
        if maximal_localisation:
            parameters.update({
                'num_iter': default_num_iter,
                'conv_tol': 1e-7 * num_atoms,
                'conv_window': 3,
            })
        else:
            parameters['num_iter'] = 0

        default_dis_num_iter = 4000
        if disentanglement_type == WannierDisentanglementType.NONE:
            parameters['dis_num_iter'] = 0
        elif disentanglement_type == WannierDisentanglementType.SMV:
            if frozen_type == WannierFrozenType.ENERGY_FIXED:
                parameters.update({
                    'dis_num_iter': default_dis_num_iter,
                    'dis_conv_tol': parameters['conv_tol'],
                    # Here +2 means fermi_energy + 2 eV, however Fermi energy is calculated when Wannier90WorkChain
                    # is running, so it will add Fermi energy with this dis_froz_max dynamically.
                    'dis_froz_max': +2.0,
                })
            elif frozen_type == WannierFrozenType.ENERGY_AUTO:
                # ENERGY_AUTO needs projectability, will be set dynamically when workchain is running
                parameters.update({
                    'dis_num_iter': default_dis_num_iter,
                    'dis_conv_tol': parameters['conv_tol'],
                })
            elif frozen_type == WannierFrozenType.PROJECTABILITY:
                parameters.update({
                    'dis_num_iter': default_dis_num_iter,
                    'dis_conv_tol': parameters['conv_tol'],
                    'dis_proj_min': 0.01,
                    'dis_proj_max': 0.95,
                })
            elif frozen_type == WannierFrozenType.FIXED_PLUS_PROJECTABILITY:
                parameters.update({
                    'dis_num_iter': default_dis_num_iter,
                    'dis_conv_tol': parameters['conv_tol'],
                    'dis_proj_min': 0.01,
                    'dis_proj_max': 0.95,
                    'dis_froz_max': +2.0,  # relative to fermi_energy
                })
            else:
                raise ValueError(f'Not supported frozen type: {frozen_type}')
        else:
            raise ValueError(f'Not supported disentanglement type: {disentanglement_type}')

        if retrieve_hamiltonian:
            parameters['write_tb'] = True
            parameters['write_hr'] = True
            parameters['write_xyz'] = True

        # if inputs.kpoints is a kmesh, mp_grid will be auto-set,
        # otherwise we need to set it manually
        # if self.inputs.use_opengrid:
        # kpoints will be set dynamically after opengrid calculation,
        # the self.ctx.nscf_kpoints won't be used.
        # inputs['kpoints'] = self.ctx.nscf_kpoints
        # else:
        kpoints = create_kpoints_from_distance(structure, kpoints_distance)
        inputs['kpoints'] = get_explicit_kpoints(kpoints)
        parameters['mp_grid'] = kpoints.get_kpoints_mesh()[0]

        inputs['parameters'] = orm.Dict(dict=parameters)
        inputs['metadata'] = {'options': {'resources': {'num_machines': 1}}}

        if retrieve_hamiltonian:
            # tbmodels needs aiida.win file
            inputs['settings'].update({'additional_retrieve_list': ['*.win']})

        if retrieve_matrices:
            # also retrieve .chk file in case we need it later
            seedname = Wannier90Calculation._DEFAULT_INPUT_FILE.split('.', maxsplit=1)[0]  # pylint: disable=protected-access
            retrieve_list = inputs['settings']['additional_retrieve_list']
            retrieve_list += [f'{seedname}.{ext}' for ext in ['chk', 'eig', 'amn', 'mmn', 'spn']]
            inputs['settings']['additional_retrieve_list'] = retrieve_list
        # I need to convert settings into orm.Dict
        inputs['settings'] = orm.Dict(dict=inputs['settings'])

        return inputs

    @classmethod
    def get_builder_from_protocol(  # pylint: disable=unused-argument,too-many-statements
        cls,
        codes: dict,
        structure: orm.StructureData,
        *,
        protocol: str = None,
        overrides: dict = None,
        pseudo_family: str = None,
        electronic_type: ElectronicType = ElectronicType.METAL,
        spin_type: SpinType = SpinType.NONE,
        initial_magnetic_moments: dict = None,
        projection_type: WannierProjectionType = WannierProjectionType.SCDM,
        disentanglement_type: WannierDisentanglementType = None,
        frozen_type: WannierFrozenType = None,
        maximal_localisation: bool = True,
        exclude_semicores: bool = True,
        plot_wannier_functions: bool = False,
        retrieve_hamiltonian: bool = False,
        retrieve_matrices: bool = False,
        print_summary: bool = True,
        summary: dict = None,
        **kwargs
    ) -> ProcessBuilder:
        """Return a builder prepopulated with inputs selected according to the chosen protocol.

        The builder can be submitted directly by `aiida.engine.submit(builder)`.

        :param codes: a dictionary of ``Code`` instance for pw.x, pw2wannier90.x, wannier90.x, (optionally) projwfc.x.
        :type codes: dict
        :param structure: the ``StructureData`` instance to use.
        :type structure: orm.StructureData
        :param protocol: protocol to use, if not specified, the default will be used.
        :type protocol: str
        :param overrides: optional dictionary of inputs to override the defaults of the protocol.
        :param electronic_type: indicate the electronic character of the system through ``ElectronicType`` instance.
        :param spin_type: indicate the spin polarization type to use through a ``SpinType`` instance.
        :param initial_magnetic_moments: optional dictionary that maps the initial magnetic moment of
        each kind to a desired value for a spin polarized calculation.
        Note that for ``spin_type == SpinType.COLLINEAR`` an initial guess for the magnetic moment
        is automatically set in case this argument is not provided.
        :param projection_type: indicate the Wannier initial projection type of the system
        through ``WannierProjectionType`` instance.
        Default to SCDM.
        :param disentanglement_type: indicate the Wannier disentanglement type of the system through
        ``WannierDisentanglementType`` instance. Default to None, which will choose the best type
        based on `projection_type`:
            For WannierProjectionType.SCDM, use WannierDisentanglementType.NONE
            For other WannierProjectionType, use WannierDisentanglementType.SMV
        :param frozen_type: indicate the Wannier disentanglement type of the system
        through ``WannierFrozenType`` instance. Default to None, which will choose
        the best frozen type based on `electronic_type` and `projection_type`.
            for ElectronicType.INSULATOR, use WannierFrozenType.NONE
            for metals or insulators with conduction bands:
                for WannierProjectionType.ANALYTIC/RANDOM, use WannierFrozenType.ENERGY_FIXED
                for WannierProjectionType.ATOMIC_PROJECTORS_QE/OPENMX, use WannierFrozenType.FIXED_PLUS_PROJECTABILITY
                for WannierProjectionType.SCDM, use WannierFrozenType.NONE
        :param maximal_localisation: if true do maximal localisation of Wannier functions.
        :param exclude_semicores: if True do not Wannierise semicore states.
        :param plot_wannier_functions: if True plot Wannier functions as xsf files.
        :param retrieve_hamiltonian: if True retrieve Wannier Hamiltonian.
        :param retrieve_matrices: if True retrieve amn/mmn/eig/chk/spin files.
        :param print_summary: if True print a summary of key input parameters
        :param summary: A dict containing key input parameters and can be printed out
        when the `get_builder_from_protocol` returns, to let user have a quick check of the
        generated inputs. Since in python dict is pass-by-reference, the input dict can be
        modified in the method and used by the invoking function. This allows printing the
        summary only by the last overriding method.
        :return: a process builder instance with all inputs defined and ready for launch.
        :rtype: ProcessBuilder
        """
        # from aiida_quantumespresso.workflows.protocols.utils import get_starting_magnetization

        # check function arguments
        codes_required_keys = ['pw', 'pw2wannier90', 'wannier90']
        codes_optional_keys = ['projwfc', 'opengrid']
        if not isinstance(codes, dict):
            msg = f"`codes` must be a dict with the following required keys: `{'`, `'.join(codes_required_keys)}` "
            msg += f"and optional keys: `{'`, `'.join(codes_optional_keys)}`"
            raise ValueError(msg)
        for k in codes_required_keys:
            if k not in codes.keys():
                raise ValueError(f'`codes` does not contain the required key: {k}')
        for k, code in codes.items():
            if isinstance(code, str):
                code = orm.load_code(code)
                type_check(code, orm.Code)
                codes[k] = code

        type_check(electronic_type, ElectronicType)
        type_check(spin_type, SpinType)
        type_check(projection_type, WannierProjectionType)
        if disentanglement_type is not None:
            type_check(disentanglement_type, WannierDisentanglementType)
        if frozen_type is not None:
            type_check(frozen_type, WannierFrozenType)

        if electronic_type not in [ElectronicType.METAL, ElectronicType.INSULATOR]:
            raise NotImplementedError(f'electronic type `{electronic_type}` is not supported.')

        if spin_type not in [SpinType.NONE, SpinType.SPIN_ORBIT]:
            raise NotImplementedError(f'spin type `{spin_type}` is not supported.')

        if initial_magnetic_moments is not None and spin_type is not SpinType.COLLINEAR:
            raise ValueError(f'`initial_magnetic_moments` is specified but spin type `{spin_type}` is incompatible.')

        # automatically set disentanglement and frozen types
        if electronic_type == ElectronicType.INSULATOR:
            if disentanglement_type is None:
                disentanglement_type = WannierDisentanglementType.NONE
            elif disentanglement_type == WannierDisentanglementType.NONE:
                pass
            else:
                raise ValueError((
                    'For insulators there should be no disentanglement, ' +
                    f'current disentanglement type: {disentanglement_type}'
                ))
            if frozen_type is None:
                frozen_type = WannierFrozenType.NONE
            elif frozen_type == WannierFrozenType.NONE:
                pass
            else:
                raise ValueError(f'For insulators there should be no frozen states, current frozen type: {frozen_type}')
        elif electronic_type == ElectronicType.METAL:
            if projection_type == WannierProjectionType.SCDM:
                if disentanglement_type is None:
                    # No disentanglement when using SCDM, otherwise the wannier interpolated bands are wrong
                    disentanglement_type = WannierDisentanglementType.NONE
                elif disentanglement_type == WannierDisentanglementType.NONE:
                    pass
                else:
                    raise ValueError((
                        'For SCDM there should be no disentanglement, ' +
                        f'current disentanglement type: {disentanglement_type}'
                    ))
                if frozen_type is None:
                    frozen_type = WannierFrozenType.NONE
                elif frozen_type == WannierFrozenType.NONE:
                    pass
                else:
                    raise ValueError(f'For SCDM there should be no frozen states, current frozen type: {frozen_type}')
            elif projection_type in [WannierProjectionType.ANALYTIC, WannierProjectionType.RANDOM]:
                if disentanglement_type is None:
                    disentanglement_type = WannierDisentanglementType.SMV
                if frozen_type is None:
                    frozen_type = WannierFrozenType.ENERGY_FIXED
                if disentanglement_type == WannierDisentanglementType.NONE and frozen_type != WannierFrozenType.NONE:
                    raise ValueError(
                        f'Disentanglement is explicitly disabled but frozen type {frozen_type} is required'
                    )
            elif projection_type in [
                WannierProjectionType.ATOMIC_PROJECTORS_QE, WannierProjectionType.ATOMIC_PROJECTORS_OPENMX
            ]:
                if disentanglement_type is None:
                    disentanglement_type = WannierDisentanglementType.SMV
                if frozen_type is None:
                    frozen_type = WannierFrozenType.FIXED_PLUS_PROJECTABILITY
                if disentanglement_type == WannierDisentanglementType.NONE and frozen_type != WannierFrozenType.NONE:
                    raise ValueError(
                        f'Disentanglement is explicitly disabled but frozen type {frozen_type} is required'
                    )
            else:
                if disentanglement_type is None or frozen_type is None:
                    raise ValueError((
                        'Cannot automatically guess disentanglement and frozen types ' +
                        f'from projection type: {projection_type}'
                    ))
        else:
            raise ValueError(f'Not supported electronic type {electronic_type}')

        if pseudo_family is None:
            if spin_type == SpinType.SPIN_ORBIT:
                # I use pseudo-dojo for SOC
                pseudo_family = 'PseudoDojo/0.4/PBE/FR/standard/upf'
            else:
                # I use aiida-qe default
                pseudo_family = PwBaseWorkChain.get_protocol_inputs(protocol=protocol)['pseudo_family']

        # A dictionary containing key info of Wannierisation and will be printed when the function returns.
        if summary is None:
            summary = {}
        summary['Formula'] = structure.get_formula()
        summary['ElectronicType'] = electronic_type
        summary['SpinType'] = spin_type
        summary['PseudoFamily'] = pseudo_family
        summary['WannierProjectionType'] = projection_type.name
        summary['WannierDisentanglementType'] = disentanglement_type.name
        summary['WannierFrozenType'] = frozen_type.name

        inputs = cls.get_protocol_inputs(protocol, overrides)
        inputs = AttributeDict(inputs)

        kpoints_distance = inputs.pop('kpoints_distance')
        nbands_factor = inputs.pop('nbands_factor')

        builder = cls.get_builder()
        builder.structure = structure

        # This will be used in various WorkChain.get_builder_from_protocol(...)
        filtered_kwargs = dict(
            structure=structure,
            protocol=protocol,
            electronic_type=electronic_type,
            spin_type=spin_type,
            pseudo_family=pseudo_family,
            initial_magnetic_moments=initial_magnetic_moments
        )

        # relax
        if inputs.get('relax', False):
            builder.relax = cls.get_relax_inputs(codes['pw'], kpoints_distance, **filtered_kwargs)

        # scf
        if inputs.get('scf', True):
            builder.scf = cls.get_scf_inputs(codes['pw'], kpoints_distance, **filtered_kwargs)

        # nscf
        if inputs.get('nscf', True):
            builder.nscf = cls.get_nscf_inputs(codes['pw'], kpoints_distance, nbands_factor, **filtered_kwargs)

        # projwfc
        run_projwfc = inputs.get('projwfc', True)
        if projection_type == WannierProjectionType.SCDM:  # pylint: disable=simplifiable-if-statement
            run_projwfc = True
        else:
            if frozen_type == WannierFrozenType.ENERGY_AUTO:  # pylint: disable=simplifiable-if-statement
                run_projwfc = True
            else:
                run_projwfc = False
        if run_projwfc:
            builder.projwfc = cls.get_projwfc_inputs(codes['projwfc'], **filtered_kwargs)

        # pw2wannier90
        if inputs.get('pw2wannier90', True):
            exclude_pswfcs = None
            if exclude_semicores:
                pseudo_orbitals = get_pseudo_orbitals(builder.scf['pw']['pseudos'])
                exclude_pswfcs = get_semicore_list(structure, pseudo_orbitals)
            pw2wannier_inputs = cls.get_pw2wannier90_inputs(
                code=codes['pw2wannier90'],
                projection_type=projection_type,
                exclude_pswfcs=exclude_pswfcs,
                plot_wannier_functions=plot_wannier_functions,
                **filtered_kwargs
            )
            builder.pw2wannier90 = {'pw2wannier90': pw2wannier_inputs}

        # wannier90
        if inputs.get('wannier90', True):
            wannier_inputs = cls.get_wannier90_inputs(
                code=codes['wannier90'],
                projection_type=projection_type,
                disentanglement_type=disentanglement_type,
                frozen_type=frozen_type,
                kpoints_distance=kpoints_distance,
                nbands=builder.nscf['pw']['parameters']['SYSTEM']['nbnd'],
                pseudos=builder.scf['pw']['pseudos'],
                maximal_localisation=maximal_localisation,
                exclude_semicores=exclude_semicores,
                plot_wannier_functions=plot_wannier_functions,
                retrieve_hamiltonian=retrieve_hamiltonian,
                retrieve_matrices=retrieve_matrices,
                **filtered_kwargs
            )
            builder.wannier90 = {'wannier90': wannier_inputs}
            builder.relative_dis_windows = orm.Bool(True)

        builder.clean_workdir = orm.Bool(inputs.get('clean_workdir', False))

        wannier_params = builder.wannier90['wannier90']['parameters'].get_dict()
        summary['num_bands'] = wannier_params['num_bands']
        summary['num_wann'] = wannier_params['num_wann']
        if 'exclude_bands' in wannier_params:
            summary['exclude_bands'] = wannier_params['exclude_bands']
        summary['mp_grid'] = wannier_params['mp_grid']

        notes = summary.get('notes', [])
        notes.extend([(
            'The `relative_dis_windows` = True, meaning the `dis_froz/win_min/max` in the '
            'wannier90 input parameters will be shifted by Fermi energy from scf output parameters.'
        ),
                      (
                          'If you set `scdm_mu` and/or `scdm_sigma` in the pw2wannier90 input parameters, '
                          'the WorkChain will directly use the provided mu and/or sigma. '
                          'The missing one will be generated from projectability.'
                      )])
        summary['notes'] = notes

        if print_summary:
            cls.print_summary(summary)

        return builder

    @classmethod
    def print_summary(cls, summary):
        """Try to pretty print the summary when the `get_builder_from_protocol` returns."""
        notes = summary.pop('notes', [])

        print('Summary of key input parameters:')
        for key, val in summary.items():
            print(f'  {key}: {val}')
        print('')

        if len(notes) == 0:
            return

        print('Notes:')
        for note in notes:
            print(f'  * {note}')

        return


def get_fermi_energy(output_parameters: orm.Dict) -> ty.Optional[float]:
    """Get Fermi energy from scf output parameters.

    :param output_parameters: scf output parameters
    :type output_parameters: orm.Dict
    :return: if found return Fermi energy, else None. Unit is eV.
    :rtype: float, None
    """
    out_dict = output_parameters.get_dict()
    fermi = out_dict.get('fermi_energy', None)
    fermi_units = out_dict.get('fermi_energy_units', None)

    if fermi_units != 'eV':
        return None

    return fermi


@calcfunction
def update_scdm_mu_sigma(
    parameters: orm.Dict, bands: orm.BandsData, projections: orm.ProjectionData, sigma_factor: orm.Float
) -> orm.Dict:
    """Use erfc fitting to extract `scdm_mu` & `scdm_sigma`, and update the pw2wannier90 input parameters.

    If `scdm_mu`/`sigma` is provided in the input, then it will not be updated,
    only the missing one(s) will be updated.

    :param parameters: pw2wannier90 input parameters
    :type parameters: aiida.orm.Dict
    :param bands: band structure
    :type bands: aiida.orm.BandsData
    :param projections: projectability from projwfc.x
    :type projections: aiida.orm.ProjectionData
    :param sigma_factor: sigma shift factor
    :type sigma_factor: aiida.orm.Float
    """
    parameters_dict = parameters.get_dict()
    mu_new, sigma_new = fit_scdm_mu_sigma_aiida(bands, projections, sigma_factor)  # pylint: disable=unbalanced-tuple-unpacking
    scdm_parameters = {}
    if 'scdm_mu' not in parameters_dict['inputpp']:
        scdm_parameters['scdm_mu'] = mu_new
    if 'scdm_sigma' not in parameters_dict['inputpp']:
        scdm_parameters['scdm_sigma'] = sigma_new
    parameters_dict['inputpp'].update(scdm_parameters)
    return orm.Dict(dict=parameters_dict)


def get_pseudo_orbitals(pseudos: ty.Mapping[str, UpfData]) -> dict:
    """Get the pseudo wavefunctions contained in the pseudopotential.

    Currently only support the following pseudopotentials installed by `aiida-pseudo`:
        1. SSSP/1.1/PBE/efficiency
        2. SSSP/1.1/PBEsol/efficiency
    """
    pseudo_data = []
    pseudo_data.append(_load_pseudo_metadata('semicore_SSSP_1.1_PBEsol_efficiency.json'))
    pseudo_data.append(_load_pseudo_metadata('semicore_SSSP_1.1_PBE_efficiency.json'))

    pseudo_orbitals = {}
    for element in pseudos:
        for data in pseudo_data:
            if data[element]['md5'] == pseudos[element].md5:
                pseudo_orbitals[element] = data[element]
                break
        else:
            raise ValueError(f'Cannot find pseudopotential {element} with md5 {pseudos[element].md5}')

    return pseudo_orbitals


def get_semicore_list(structure: orm.StructureData, pseudo_orbitals: dict) -> list:
    """Get semicore states (a subset of pseudo wavefunctions) in the pseudopotential.

    :param structure: [description]
    :type structure: orm.StructureData
    :param pseudo_orbitals: [description]
    :type pseudo_orbitals: dict
    :return: [description]
    :rtype: list
    """
    # pw2wannier90.x/projwfc.x store pseudo-wavefunctions in the same order
    # as ATOMIC_POSITIONS in pw.x input file; aiida-quantumespresso writes
    # ATOMIC_POSITIONS in the order of StructureData.sites.
    # Note some times the PSWFC in UPF files are not ordered, i.e. it's not
    # always true that the first several PSWFC are semicores states, the
    # json file which we loaded in the self.ctx.pseudo_pswfcs already
    # consider this ordering, e.g.
    # "Ce": {
    #     "filename": "Ce.GGA-PBE-paw-v1.0.UPF",
    #     "md5": "c46c5ce91c1b1c29a1e5d4b97f9db5f7",
    #     "pswfcs": ["5S", "6S", "5P", "6P", "5D", "6D", "4F", "5F"],
    #     "semicores": ["5S", "5P"]
    # }
    label2num = {'S': 1, 'P': 3, 'D': 5, 'F': 7}
    semicore_list = []  # index should start from 1
    num_pswfcs = 0
    for site in structure.sites:
        # here I use deepcopy to make sure list.remove() does not
        # interfere with the original list.
        site_pswfcs = deepcopy(pseudo_orbitals[site.kind_name]['pswfcs'])
        site_semicores = deepcopy(pseudo_orbitals[site.kind_name]['semicores'])
        for orb in site_pswfcs:
            num_orbs = label2num[orb[-1]]
            if orb in site_semicores:
                site_semicores.remove(orb)
                semicore_list.extend(list(range(num_pswfcs + 1, num_pswfcs + num_orbs + 1)))
            num_pswfcs += num_orbs
        if len(site_semicores) != 0:
            return ValueError(f'Error when processing pseudo {site.kind_name} with orbitals {pseudo_orbitals}')
    return semicore_list


def get_scf_fermi_energy(calc_nscf: ty.Union[PwBaseWorkChain, PwCalculation]) -> float:
    """Parse nscf output to get the scf Fermi energy.

    :param calc_nscf: a nscf PwBaseWorkChain or PwCalculation
    :type calc_nscf: ty.Union[PwBaseWorkChain, PwCalculation]
    :return: scf Fermi energy
    :rtype: float
    """
    import re
    from aiida_wannier90_workflows.utils.node import get_last_calcjob

    supported_inputs = (PwBaseWorkChain, PwCalculation)
    if calc_nscf.process_class not in supported_inputs:
        raise ValueError(f'Only support {supported_inputs}, input is {calc_nscf}')

    if not calc_nscf.is_finished_ok:
        raise ValueError(f'Input {calc_nscf} has not finished successfully')

    if calc_nscf.process_class == PwBaseWorkChain:
        calc_nscf = get_last_calcjob(calc_nscf)

    if calc_nscf.process_class != PwCalculation:
        raise ValueError(f'Input {calc_nscf} is not a PwCalculation')

    out = calc_nscf.outputs.retrieved.get_object_content('aiida.out')
    lines = out.split('\n')

    # QE 6.8 output scf Fermi energy in nscf run:
    #  the Fermi energy is     5.9816 ev
    #  (compare with:     5.9034 eV, computed in scf)
    fermi_energy = None
    regex = re.compile(r'\s*\(compare with:\s*([+-]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+))\s*eV, computed in scf\)')
    for line in lines:
        match = regex.match(line)
        if match:
            fermi_energy = float(match.group(1))
            break

    return fermi_energy


def get_homo_lumo(bands: np.array, fermi_energy: float) -> ty.Tuple[float, float]:
    """Find highest occupied bands and lowest unoccupied bands around Fermi energy.

    :param bands: num_kpoints * num_bands
    :type bands: np.array
    :param fermi_energy: [description]
    :type fermi_energy: float
    :return: [description]
    :rtype: ty.Tuple[float, float]
    """
    occupied = bands <= fermi_energy
    unoccupied = bands > fermi_energy

    homo = np.max(bands[occupied])
    lumo = np.min(bands[unoccupied])

    return homo, lumo