#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Plot band structures."""
import typing as ty

import matplotlib.pyplot as plt

from aiida import orm

from aiida_quantumespresso.calculations.pw import PwCalculation
from aiida_quantumespresso.workflows.pw.base import PwBaseWorkChain
from aiida_quantumespresso.workflows.pw.bands import PwBandsWorkChain

from aiida_wannier90.calculations import Wannier90Calculation

from aiida_wannier90_workflows.workflows.base.wannier90 import Wannier90BaseWorkChain
from aiida_wannier90_workflows.workflows.wannier90 import Wannier90WorkChain
from aiida_wannier90_workflows.workflows.bands import Wannier90BandsWorkChain
from aiida_wannier90_workflows.workflows.optimize import Wannier90OptimizeWorkChain
from aiida_wannier90_workflows.workflows.projwfcbands import ProjwfcBandsWorkChain


def plot_scdm_fit(workchain: int, save: bool = False):
    """Plot the projectabilities distribution of SCDM fitting."""
    from aiida_wannier90_workflows.utils.workflows import get_last_calcjob
    from aiida_wannier90_workflows.utils.scdm import erfc_scdm, fit_scdm_mu_sigma

    valid_classes = [Wannier90BandsWorkChain, Wannier90WorkChain]
    if workchain.process_class not in valid_classes:
        raise ValueError(f'Input workchain type should be {valid_classes}')

    formula = workchain.inputs.structure.get_formula()

    w90calc = workchain.get_outgoing(link_label_filter='wannier90').one().node
    p2w_workchain = workchain.get_outgoing(link_label_filter='pw2wannier90').one().node
    p2wcalc = get_last_calcjob(p2w_workchain)
    projcalc = workchain.get_outgoing(link_label_filter='projwfc').one().node

    fermi_energy = w90calc.inputs.parameters['fermi_energy']
    sigma = p2wcalc.inputs.parameters['inputpp']['scdm_sigma']
    mu = p2wcalc.inputs.parameters['inputpp']['scdm_mu']
    projections = projcalc.outputs.projections
    bands = projcalc.outputs.bands

    mu_fit, sigma_fit, data = fit_scdm_mu_sigma(bands, projections, sigma_factor=orm.Float(0), return_data=True)

    print(f'{formula:6s}:')
    print(f'        fermi_energy = {fermi_energy}, mu = {mu}, sigma = {sigma}')

    # check the fitting are consistent
    eps = 1e-6
    assert abs(sigma - sigma_fit) < eps
    sigma_factor = workchain.inputs.scdm_sigma_factor.value
    assert abs(mu - (mu_fit - sigma_fit * sigma_factor)) < eps
    sorted_bands = data[0, :]
    sorted_projwfc = data[1, :]

    plt.figure()
    plt.plot(sorted_bands, sorted_projwfc, 'o')
    plt.plot(sorted_bands, erfc_scdm(sorted_bands, mu_fit, sigma_fit))
    plt.axvline([mu_fit], color='red', label=r'$\mu$')
    plt.axvline([mu_fit - sigma_factor * sigma_fit], color='orange', label=r'$\mu-' + str(sigma_factor) + r'\sigma$')
    plt.axvline([fermi_energy], color='green', label=r'$E_f$')
    plt.title(f'{workchain.process_label}<{workchain.pk}>: {formula}')
    plt.xlabel('Energy [eV]')
    plt.ylabel('Projectability')
    plt.legend(loc='best')

    if save:
        plt.savefig(f'scdmfit_{formula}_{workchain.pk}.png')
    else:
        plt.show()


def get_mpl_code_for_bands(
    dft_bands, wan_bands, *, fermi_energy=None, shift_fermi=False, title=None, save=False, filename=None
):
    """Return matplotlib code for comparing band structures."""

    if fermi_energy is None and shift_fermi:
        raise ValueError('shift_fermi requested but no fermi_energy provided?')

    # dft_bands.show_mpl()
    legend = f'{dft_bands.pk}' if dft_bands.pk else 'DFT'
    dft_mpl_code = dft_bands._exportcontent(fileformat='mpl_singlefile', legend=legend, main_file_name='')[0]  # pylint: disable=protected-access
    legend = f'{wan_bands.pk}' if wan_bands.pk else 'W90'
    wan_mpl_code = wan_bands._exportcontent(  # pylint: disable=protected-access
        fileformat='mpl_singlefile',
        legend=legend,
        main_file_name='',
        bands_color='r',
        bands_linestyle='dashed'
    )[0]

    if fermi_energy is not None:
        replacement = f'fermi_energy = {fermi_energy}\n\n'
        if shift_fermi:
            replacement += "p.axhline(y=0, color='blue', linestyle='--', label='Fermi', zorder=-1)\n"
        else:
            replacement += "p.axhline(y=fermi_energy, color='blue', linestyle='--', label='Fermi', zorder=-1)\n"
        replacement += 'pl.legend()\n\n'
        replacement += 'for path in paths:'
        dft_mpl_code = dft_mpl_code.replace(b'for path in paths:', replacement.encode())

    dft_mpl_code = dft_mpl_code.replace(b'pl.show()', b'')

    wan_mpl_code = wan_mpl_code.replace(b'fig = pl.figure()', b'')
    wan_mpl_code = wan_mpl_code.replace(b'p = fig.add_subplot(1,1,1)', b'')

    if shift_fermi:
        dft_mpl_code = dft_mpl_code.replace(
            b'p.plot(x, band, label=label,', b'p.plot(x, [_-fermi_energy for _ in band], label=label,'
        )
        wan_mpl_code = wan_mpl_code.replace(
            b'p.plot(x, band, label=label,', b'p.plot(x, [_-fermi_energy for _ in band], label=label,'
        )
        dft_mpl_code = dft_mpl_code.replace(
            b"p.set_ylim([all_data['y_min_lim'], all_data['y_max_lim']])",
            b"p.set_ylim([all_data['y_min_lim']-fermi_energy, all_data['y_max_lim']-fermi_energy])"
        )
        wan_mpl_code = wan_mpl_code.replace(
            b"p.set_ylim([all_data['y_min_lim'], all_data['y_max_lim']])",
            b"p.set_ylim([all_data['y_min_lim']-fermi_energy, all_data['y_max_lim']-fermi_energy])"
        )

    mpl_code = dft_mpl_code + wan_mpl_code

    if title is None:
        title = f'1st bands pk {dft_bands.pk}, 2nd bands pk {wan_bands.pk}'
    replacement = f'p.set_title("{title}")\npl.show()'
    mpl_code = mpl_code.replace(b'pl.show()', replacement.encode())

    if save:
        if filename is None:
            filename = f'bandsdiff_{dft_bands.pk}_{wan_bands.pk}.py'
        with open(filename, 'w') as handle:
            handle.write(mpl_code.decode())

    return mpl_code


def get_mpl_code_for_workchains(workchain0, workchain1, title=None, save=False, filename=None):
    """Return matplotlib code for comparing band structures of two workchains."""

    def get_output_bands(workchain):
        if workchain.process_class == PwBaseWorkChain:
            return workchain.outputs.output_band
        if workchain.process_class in (
            PwBandsWorkChain, ProjwfcBandsWorkChain, Wannier90BandsWorkChain, Wannier90OptimizeWorkChain
        ):
            return workchain.outputs.band_structure
        if workchain.process_class in (Wannier90Calculation, Wannier90BaseWorkChain):
            return workchain.outputs.interpolated_bands
        raise ValueError(f'Unrecognized workchain type: {workchain}')

    # assume workchain0 is pw, workchain1 is wannier
    dft_bands = get_output_bands(workchain0)
    wan_bands = get_output_bands(workchain1)

    if workchain1.process_class in (Wannier90BaseWorkChain,):
        formula = workchain1.inputs.wannier90.structure.get_formula()
    else:
        formula = workchain1.inputs.structure.get_formula()
    if title is None:
        title = f'{formula}, {workchain0.process_label}<{workchain0.pk}> bands<{dft_bands.pk}>, '
        title += f'{workchain1.process_label}<{workchain1.pk}> bands<{wan_bands.pk}>'

    if save and (filename is None):
        filename = f'bandsdiff_{formula}_{workchain0.pk}_{workchain1.pk}.py'

    if workchain1.process_class in (Wannier90BaseWorkChain, Wannier90BandsWorkChain, Wannier90OptimizeWorkChain):
        fermi_energy = get_workchain_fermi_energy(workchain1)
    else:
        if workchain0.process_class == PwBandsWorkChain:
            fermi_energy = workchain0.outputs['scf_parameters']['fermi_energy']
        else:
            raise ValueError('Cannot find fermi energy')

    mpl_code = get_mpl_code_for_bands(
        dft_bands, wan_bands, fermi_energy=fermi_energy, shift_fermi=False, title=title, save=save, filename=filename
    )

    return mpl_code


def get_workchain_fermi_energy(workchain: ty.Union[Wannier90BaseWorkChain, Wannier90BandsWorkChain]) -> float:
    """Get Fermi energy of Wannier90BandsWorkChain.

    :param workchain: [description]
    :type workchain: Wannier90BandsWorkChain
    :return: [description]
    :rtype: float
    """
    from aiida_wannier90_workflows.utils.workflows import get_last_calcjob
    from aiida_wannier90_workflows.utils.workflows.pw import get_fermi_energy, get_fermi_energy_from_nscf

    if 'scf' in workchain.outputs:
        fermi_energy = get_fermi_energy(workchain.outputs['scf']['output_parameters'])
    else:
        if workchain.process_class in (PwBaseWorkChain, PwCalculation):
            pw_calc = get_last_calcjob(workchain)
            fermi_energy = get_fermi_energy_from_nscf(pw_calc)
        else:
            if workchain.process_class == Wannier90BaseWorkChain:
                w90calc = get_last_calcjob(workchain)
            elif workchain.process_class in (Wannier90BandsWorkChain, Wannier90OptimizeWorkChain):
                w90calc = get_last_calcjob(workchain.get_outgoing(link_label_filter='wannier90').one().node)
            else:
                raise ValueError('Cannot find fermi energy')

            if 'fermi_energy' in w90calc.inputs.parameters.get_dict():
                fermi_energy = w90calc.inputs.parameters.get_dict()['fermi_energy']
            else:
                raise ValueError('Cannot find fermi energy')

    return fermi_energy


def get_mapping_for_group(
    wan_group: ty.Union[str, orm.Group],
    dft_group: ty.Union[str, orm.Group],
    match_by_formula: bool = False
) -> ty.Dict[orm.Node, orm.Node]:
    """Find the corresponding DFT workchain for each Wannier workchain.

    :param wan_group: group label of ``Wannier90BandsWorkChain``
    :type wan_group: str
    :param dft_group: group label of ``PwBandsWorkChain``
    :type dft_group: str
    :param match_by_formula: match by structure formula or structure node itself, defaults to False
    :type match_by_formula: bool, optional
    :return: A dict with ``Wannier90BandsWorkChain`` as key and the corresponding ``PwBandsWorkChain`` as
    value. If not found the value is ``None``.
    :rtype: dict
    """
    if isinstance(wan_group, str):
        wan_group = orm.load_group(wan_group)
    if isinstance(dft_group, str):
        dft_group = orm.load_group(dft_group)

    if len(dft_group.nodes) == 0:
        print(f'DFT group<{dft_group.pk}> is empty')
        return None

    if len(wan_group.nodes) == 0:
        print(f'Wannier group<{wan_group.pk}> is empty')
        return None

    # PwBandsWorkChain calls seekpath, I need to use seekpath reduced structure
    if dft_group.nodes[0].process_class == PwBandsWorkChain:
        dft_structures = {_.outputs.primitive_structure: _ for _ in dft_group.nodes}
    else:
        # Just check the input structure
        if 'structure' in dft_group.nodes[0].inputs:
            dft_structures = {_.inputs.structure: _ for _ in dft_group.nodes}
        elif 'structure' in dft_group.nodes[0].inputs['pw']:
            dft_structures = {_.inputs.pw.structure: _ for _ in dft_group.nodes}
    if match_by_formula:
        dft_structures = {k.get_formula(): v for k, v in dft_structures.items()}
    # print(f'Found DFT calculations: {dft_structures}')

    mapping = {}
    for wan_wc in wan_group.nodes:
        structure = wan_wc.inputs.structure
        formula = structure.get_formula()

        try:
            if match_by_formula:
                dft_wc = dft_structures[formula]
            else:
                dft_wc = dft_structures[structure]
            mapping[wan_wc] = dft_wc
        except KeyError:
            mapping[wan_wc] = None

    return mapping


def export_bands_for_group(
    wan_group: ty.Union[str, orm.Group],
    dft_group: ty.Union[str, orm.Group],
    save_dir: str,
    match_by_formula: bool = False
):
    """Export matplotlib code for comparing DFT and Wannier bands in two groups.

    :param wan_group: [description]
    :type wan_group: ty.Union[str, orm.Group]
    :param dft_group: [description]
    :type dft_group: ty.Union[str, orm.Group]
    :param save_dir: [description]
    :type save_dir: str
    :param match_by_formula: [description], defaults to False
    :type match_by_formula: bool, optional
    """
    import os.path

    if isinstance(wan_group, str):
        wan_group = orm.load_group(wan_group)
    if isinstance(dft_group, str):
        dft_group = orm.load_group(dft_group)

    mapping = get_mapping_for_group(wan_group, dft_group, match_by_formula)

    if not os.path.exists(save_dir):
        os.mkdir(save_dir)
    os.chdir(save_dir)
    print(f'files are saved in {save_dir}')

    for wan_wc in wan_group.nodes:
        if not wan_wc.is_finished_ok:
            print(f'! Skip unfinished {wan_wc.process_label}<{wan_wc.pk}> of {formula}')
            continue

        dft_wc = mapping[wan_wc]
        if dft_wc is None:
            msg = f'! Cannot find DFT bands for {wan_wc.process_label}<{wan_wc.pk}> of {formula}'
            print(msg)
            continue

        if not dft_wc.is_finished_ok:
            print(f'! Skip unfinished DFT {dft_wc.process_label}<{dft_wc.pk}> of {formula}')
            continue
        dft_bands = dft_wc.outputs.output_band

        formula = wan_wc.inputs.structure.get_formula()
        filename = f'bandsdiff_{formula}_{wan_wc.pk}.py'

        if wan_wc.process_class == Wannier90Calculation:
            fermi_energy = wan_wc.inputs.parameters['fermi_energy']
            w90_bands = wan_wc.outputs.band_structure
            get_mpl_code_for_bands(dft_bands, w90_bands, fermi_energy=fermi_energy, save=True, filename=filename)
        else:
            get_mpl_code_for_workchains(dft_wc, wan_wc, save=True, filename=filename)


def bands_py_to_png(py_dir: str, png_dir: str):
    """Convert ``bandsdiff_*.py`` files generated by ``export_bands_for_group`` to png files.

    :param py_dir: directory of ``*.py`` files
    :type py_dir: str
    :param png_dir: directory to save png files
    :type png_dir: str
    """
    import glob

    py_pattern = 'bandsdiff_*.py'
    print(f'Searching {py_pattern} in {py_dir}, save png in {png_dir}')

    globbed = glob.glob(f'{py_dir}/{py_pattern}')
    for filename in globbed:
        with open(filename) as handle:
            mplcode = ''.join(handle.readlines())
            mplcode = mplcode.replace('fig = pl.figure()', 'fig = pl.figure(figsize=(16,10))')
            png_filename = filename.removesuffix('.py') + '.png'
            print(f'{py_dir}/{filename} -> {png_dir}/{png_filename}')
            mplcode = mplcode.replace('pl.show()', f"pl.savefig('{png_dir}/{png_filename}', bbox_inches='tight')")
            exec(mplcode)  # pylint: disable=exec-used


def get_band_dict(band: ty.Union[dict, orm.BandsData], /) -> dict:
    """Get a dictonary of BandsData.

    :param band: _description_
    :type band: ty.Union[dict, orm.BandsData]
    :raises ValueError: _description_
    :return: _description_
    :rtype: dict
    """
    if isinstance(band, dict):
        return band

    if isinstance(band, orm.BandsData):
        # band_json, _ = band._exportcontent(fileformat='json')
        # # band_json = band_json.decode()
        # band_dict = json.loads(band_json)
        band_dict = band._matplotlib_get_dict()  # pylint: disable=protected-access
        return band_dict

    raise ValueError(f'Unsupported type {band}')


def plot_band(  # pylint: disable=too-many-statements
    band: ty.Union[dict, orm.BandsData],
    ref_zero: float = 0,
    ax=None,
):
    """Plot aiida exported json bands.

    :param band: _description_
    :type band: dict
    :param ref_zero: _description_, defaults to 0
    :type ref_zero: float, optional
    :param ax: _description_, defaults to None
    :type ax: _type_, optional
    """
    from matplotlib import rc

    if ref_zero is None:
        ref_zero = 0

    # Uncomment to change default font
    #rc('font',**{'family':'sans-serif','sans-serif':['Helvetica']})
    rc('font', **{'family': 'serif', 'serif': ['Computer Modern', 'CMU Serif', 'Times New Roman', 'DejaVu Serif']})
    # To use proper font for, e.g., Gamma if usetex is set to False
    rc('mathtext', fontset='cm')

    rc('text', usetex=True)
    plt.rcParams.update({'text.latex.preview': True})

    print_comment = False

    all_data = get_band_dict(band)

    if not all_data.get('use_latex', False):
        rc('text', usetex=False)

    #x = all_data['x']
    #bands = all_data['bands']
    paths = all_data['paths']
    tick_pos = all_data['tick_pos']
    tick_labels = all_data['tick_labels']

    # Option for bands (all, or those of type 1 if there are two spins)
    further_plot_options1 = {}
    further_plot_options1['color'] = all_data.get('bands_color', 'k')
    further_plot_options1['linewidth'] = all_data.get('bands_linewidth', 0.5)
    further_plot_options1['linestyle'] = all_data.get('bands_linestyle', None)
    further_plot_options1['marker'] = all_data.get('bands_marker', None)
    further_plot_options1['markersize'] = all_data.get('bands_markersize', None)
    further_plot_options1['markeredgecolor'] = all_data.get('bands_markeredgecolor', None)
    further_plot_options1['markeredgewidth'] = all_data.get('bands_markeredgewidth', None)
    further_plot_options1['markerfacecolor'] = all_data.get('bands_markerfacecolor', None)

    # Options for second-type of bands if present (e.g. spin up vs. spin down)
    further_plot_options2 = {}
    further_plot_options2['color'] = all_data.get('bands_color2', 'r')
    # Use the values of further_plot_options1 by default
    further_plot_options2['linewidth'] = all_data.get('bands_linewidth2', further_plot_options1['linewidth'])
    further_plot_options2['linestyle'] = all_data.get('bands_linestyle2', further_plot_options1['linestyle'])
    further_plot_options2['marker'] = all_data.get('bands_marker2', further_plot_options1['marker'])
    further_plot_options2['markersize'] = all_data.get('bands_markersize2', further_plot_options1['markersize'])
    further_plot_options2['markeredgecolor'] = all_data.get(
        'bands_markeredgecolor2', further_plot_options1['markeredgecolor']
    )
    further_plot_options2['markeredgewidth'] = all_data.get(
        'bands_markeredgewidth2', further_plot_options1['markeredgewidth']
    )
    further_plot_options2['markerfacecolor'] = all_data.get(
        'bands_markerfacecolor2', further_plot_options1['markerfacecolor']
    )

    if ax is None:
        fig = plt.figure()
        p = fig.add_subplot(1, 1, 1)  # pylint: disable=invalid-name
    else:
        p = ax  # pylint: disable=invalid-name

    first_band_1 = True
    first_band_2 = True

    for path in paths:
        if path['length'] <= 1:
            # Avoid printing empty lines
            continue
        x = path['x']
        #for band in bands:
        # pylint: disable=redefined-argument-from-local
        for band, band_type in zip(path['values'], all_data['band_type_idx']):

            # For now we support only two colors
            if band_type % 2 == 0:
                further_plot_options = further_plot_options1
            else:
                further_plot_options = further_plot_options2

            # Put the legend text only once
            label = None
            if first_band_1 and band_type % 2 == 0:
                first_band_1 = False
                label = all_data.get('legend_text', None)
            elif first_band_2 and band_type % 2 == 1:
                first_band_2 = False
                label = all_data.get('legend_text2', None)

            p.plot(x, [_ - ref_zero for _ in band], label=label, **further_plot_options)

    p.set_xticks(tick_pos)
    p.set_xticklabels(tick_labels)
    p.set_xlim([all_data['x_min_lim'], all_data['x_max_lim']])
    p.set_ylim([all_data['y_min_lim'] - ref_zero, all_data['y_max_lim'] - ref_zero])
    p.xaxis.grid(True, which='major', color='#888888', linestyle='-', linewidth=0.5)

    if all_data.get('plot_zero_axis', False):
        p.axhline(
            0.,
            color=all_data.get('zero_axis_color', '#888888'),
            linestyle=all_data.get('zero_axis_linestyle', '--'),
            linewidth=all_data.get('zero_axis_linewidth', 0.5),
        )
    if all_data['title']:
        p.set_title(all_data['title'])
    if all_data['legend_text']:
        p.legend(loc='best')
    p.set_ylabel(all_data['yaxis_label'])

    try:
        if print_comment:
            print(all_data['comment'])
    except KeyError:
        pass

    if ax is None:
        plt.show()


def plot_bands_diff(
    qe: ty.Union[dict, orm.BandsData],
    w90: ty.Union[dict, orm.BandsData],
    fermi_energy: float = None,
    dis_froz_max: float = None,
    ax: plt.Axes = None,
    save: bool = False,
    filename: str = None,
):
    """Plot bands difference.

    :param qe: _description_
    :type qe: ty.Union[dict, orm.BandsData]
    :param w90: _description_
    :type w90: ty.Union[dict, orm.BandsData]
    :param fermi_energy: _description_, defaults to None
    :type fermi_energy: float, optional
    :param dis_froz_max: _description_, defaults to None
    :type dis_froz_max: float, optional
    :param ax: _description_, defaults to None
    :type ax: plt.Axes, optional
    :param save: _description_, defaults to False
    :type save: bool, optional
    :param filename: _description_, defaults to None
    :type filename: str, optional
    """

    show_fig = False
    if ax is None:
        show_fig = True
        _, ax = plt.subplots()
    if save:
        show_fig = False
        if filename is None:
            filename = f'bandsdiff-{qe}-{w90}.png'

    qe = get_band_dict(qe)
    w90 = get_band_dict(w90)

    qe['yaxis_label'] = 'E (eV)'
    qe['legend_text'] = 'QE'
    qe['plot_zero_axis'] = True

    plot_band(qe, ref_zero=fermi_energy, ax=ax)

    w90['yaxis_label'] = 'E (eV)'
    w90['legend_text'] = 'W90'
    w90['bands_color'] = 'red'
    # w90['bands_linestyle'] = 'dashed'

    plot_band(w90, ref_zero=fermi_energy, ax=ax)

    ax.legend(loc='lower right')

    # dis_froz_max, relative to Ef
    if dis_froz_max is not None:
        y = dis_froz_max
        if fermi_energy is not None:
            y -= fermi_energy
        ax.axhline(
            y,
            color='b',
            linestyle='--',
            linewidth=0.5,
            zorder=-1,
        )

    plt.autoscale(axis='y')

    if save:
        ax.figure.savefig(filename)

    if show_fig:
        plt.show()

    # plt.close(ax.figure)


def get_workflow_output_band(
    node: ty.Union[PwBandsWorkChain, Wannier90BandsWorkChain, Wannier90OptimizeWorkChain, ProjwfcBandsWorkChain,
                   orm.BandsData], /
) -> orm.BandsData:
    """Get BandsData of workchain outputs.

    :param node: _description_
    :type node: ty.Union[PwBandsWorkChain, Wannier90BandsWorkChain,
    Wannier90OptimizeWorkChain, ProjwfcBandsWorkChain, orm.BandsData]
    :raises ValueError: _description_
    :return: _description_
    :rtype: orm.BandsData
    """
    if isinstance(node, orm.BandsData):
        return node

    # procss_class = node.process_class

    # # print(f'{procss_class=}')

    # if procss_class in [
    #         PwBandsWorkChain, ProjwfcBandsWorkChain, Wannier90BandsWorkChain,
    #         Wannier90OptimizeWorkChain,
    # ]:
    #     band = node.outputs.band_structure
    # elif procss_class in [PwBaseWorkChain]:
    #     band = node.outputs.output_band
    # elif procss_class in [Wannier90BaseWorkChain, Wannier90Calculation]:
    #     band = node.outputs.interpolated_bands
    # else:
    #     raise ValueError(f'Unsupported workflow type {node}')
    # return band

    if isinstance(node, orm.WorkflowNode):
        for out in ['band_structure', 'output_band', 'interpolated_bands']:
            if out in node.outputs:
                return node.outputs[out]

    raise ValueError(f'Unsupported workflow type {node}')
