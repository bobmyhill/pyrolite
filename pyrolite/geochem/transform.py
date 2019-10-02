import periodictable as pt
import pandas as pd
import numpy as np
import functools
from ..util.pd import to_frame
from ..comp.codata import renormalise, close
from ..util.text import titlecase, remove_suffix
from ..util.types import iscollection
from ..util.meta import update_docstring_references
from ..util.math import OP_constants, lambdas, lambda_poly_func
from .norm import RefComp, get_reference_composition
from ..util.units import scale
from .ind import (
    REE,
    get_ionic_radii,
    simple_oxides,
    common_elements,
    common_oxides,
    __common_elements__,
    __common_oxides__,
    get_cations,
)
from .parse import check_multiple_cation_inclusion, tochem
import logging

logging.getLogger(__name__).addHandler(logging.NullHandler())
logger = logging.getLogger(__name__)


def to_molecular(df: pd.DataFrame, renorm=True):
    """
    Converts mass quantities to molar quantities of the same order. Does not convert
    units (i.e. mass% --> mol%; mass-ppm --> mol-ppm).

    Parameters
    -----------
    df : :class:`pandas.DataFrame`
        Dataframe to transform.
    renorm : :class:`bool`, :code:`True`
        Whether to renormalise the dataframe after converting to relative moles.

    Returns
    -------
    :class:`pandas.DataFrame`
        Transformed dataframe.
    """
    # df = df.to_frame()
    MWs = [pt.formula(c).mass for c in df.columns]
    if renorm:
        return renormalise(df.div(MWs))
    else:
        return df.div(MWs)


def to_weight(df: pd.DataFrame, renorm=True):
    """
    Converts molar quantities to mass quantities of the same order. Does not convert
    units (i.e. mol% --> mass%; mol-ppm --> mass-ppm).

    Parameters
    -----------
    df : :class:`pandas.DataFrame`
        Dataframe to transform.
    renorm : :class:`bool`, :code:`True`
        Whether to renormalise the dataframe after converting to relative moles.

    Returns
    -------
    :class:`pandas.DataFrame`
        Transformed dataframe.
    """
    # df = df.to_frame()
    MWs = [pt.formula(c).mass for c in df.columns]
    if renorm:
        return renormalise(df.multiply(MWs))
    else:
        return df.multiply(MWs)


def devolatilise(
    df: pd.DataFrame,
    exclude=["H2O", "H2O_PLUS", "H2O_MINUS", "CO2", "LOI"],
    renorm=True,
):
    """
    Recalculates components after exclusion of volatile phases (e.g. H2O, CO2).

    Parameters
    -----------
    df : :class:`pandas.DataFrame`
        Dataframe to devolatilise.
    exclude : :class:`list`
        Components to exclude from the dataset.
    renorm : :class:`bool`, :code:`True`
        Whether to renormalise the dataframe after devolatilisation.

    Returns
    -------
    :class:`pandas.DataFrame`
        Transformed dataframe.
    """
    keep = [i for i in df.columns if not i in exclude]
    if renorm:
        return renormalise(df.loc[:, keep])
    else:
        return df.loc[:, keep]


def oxide_conversion(oxin, oxout, molecular=False):
    """
    Factory function to generate a function to convert oxide components between
    two elemental oxides, for use in redox recalculations.

    Parameters
    ----------
    oxin : :class:`str` | :class:`~periodictable.formulas.Formula`
        Input component.
    oxout : :class:`str` | :class:`~periodictable.formulas.Formula`
        Output component.
    molecular : :class:`bool`, :code:`False`
        Whether to apply the conversion for molecular data.

    Returns
    -------
        Function to convert a :class:`pandas.Series` from one elment-oxide
        component to another.
    """
    if not isinstance(oxin, pt.formulas.Formula):
        oxin = pt.formula(oxin)
    if not isinstance(oxout, pt.formulas.Formula):
        oxout = pt.formula(oxout)

    inatoms = {k: v for (k, v) in oxin.atoms.items() if not str(k) == "O"}
    in_els = inatoms.keys()
    outatoms = {k: v for (k, v) in oxout.atoms.items() if not str(k) == "O"}
    out_els = outatoms.keys()
    try:
        assert (len(in_els) == len(out_els)) & (
            len(in_els) == 1
        )  # Assertion of simple oxide
        assert in_els == out_els  # Need to be dealilng with the same element!
    except:
        raise ValueError("Incompatible compounds: {} --> {}".format(in_els, out_els))
    # Moles of product vs. moles of reactant
    cation_coefficient = list(inatoms.values())[0] / list(outatoms.values())[0]

    def convert_series(dfser: pd.Series, molecular=molecular):
        if molecular:
            factor = cation_coefficient
        else:
            factor = cation_coefficient * oxout.mass / oxin.mass
        converted = dfser * factor
        return converted

    doc = "Convert series from " + str(oxin) + " to " + str(oxout)
    convert_series.__doc__ = doc
    return convert_series


def elemental_sum(
    df: pd.DataFrame,
    component=None,
    to=None,
    total_suffix="T",
    logdata=False,
    molecular=False,
):
    """
    Sums abundance for a cation to a single series, starting from a
    dataframe containing multiple componnents with a single set of units.

    Parameters
    ----------
    df : :class:`pandas.DataFrame`
        DataFrame for which to aggregate cation data.
    component : :class:`str`
        Component indicating which element to aggregate.
    to : :class:`str`
        Component to cast the output as.
    logdata : :class:`bool`, :code:`False`
        Whether data has been log transformed.
    molecular : :class:`bool`, :code:`False`
        Whether to perform a sum of molecular data.

    Returns
    -------
    :class:`pandas.Series`
        Series with cation aggregated.
    """
    assert component is not None
    if isinstance(component, (list, tuple, dict)):
        cations = [get_cations(t, total_suffix=total_suffix)[0] for t in component]
        assert all([c == cations[0] for c in cations])
        cation = cations[0]
    else:
        cation = get_cations(component, total_suffix=total_suffix)[0]

    cationname = str(cation)
    logger.debug("Agregating {} Data.".format(cationname))
    # different species
    poss_specs = [cationname] + simple_oxides(cation)
    poss_specs += [i + total_suffix for i in poss_specs]
    species = [i for i in set(poss_specs) if i in df.columns]
    if not species:
        logger.warning(
            "No relevant species ({}) found to aggregate.".format(poss_specs)
        )
        # return nulls
        subsum = pd.Series(np.ones(df.index.size) * np.nan, index=df.index)
    else:
        subdf = df.loc[:, species].copy(deep=True)
        if logdata:
            logger.debug("Inverse-log-transforming {} data.".format(cationname))
            subdf = subdf.applymap(np.exp)

        logger.debug(
            "Converting all {} data to metallic {} equiv.".format(
                cationname, cationname
            )
        )
        for s in species:
            form = remove_suffix(s, suffix=total_suffix)
            subdf[s] = subdf[s].apply(
                oxide_conversion(form, cationname, molecular=molecular)
            )

        logger.debug("Zeroing non-finite and negative {} values.".format(cationname))
        subdf[(~np.isfinite(subdf.values)) | (subdf < 0.0)] = 0.0
        subsum = subdf.sum(axis=1)
        subsum[subsum <= 0.0] = np.nan

    if to is None:
        subsum.name = cationname
        return subsum
    else:
        subsum.name = to
        return subsum.apply(oxide_conversion(cationname, to, molecular=molecular))


def aggregate_element(
    df: pd.DataFrame, to, total_suffix="T", logdata=False, renorm=False, molecular=False
):
    """
    Aggregates cation information from oxide and elemental components to either a
    single species or a designated mixture of species.

    Parameters
    ----------
    df : :class:`pandas.DataFrame`
        DataFrame for which to aggregate cation data.
    to : :class:`str` | :class:`~periodictable.core.Element` | :class:`~periodictable.formulas.Formula`  | :class:`dict`
        Component(s) to convert to. If one component is specified, the element will be
        converted to the target species.

        If more than one component is specified with proportions in a dictionary
        (e.g. :code:`{'FeO': 0.9, 'Fe2O3': 0.1}`), the components will be split as a
        fraction of the elemental sum.
    renorm : :class:`bool`, :code:`True`
        Whether to renormalise the dataframe after recalculation.
    total_suffix : :class:`str`, 'T'
        Suffix of 'total' variables. E.g. 'T' for FeOT, Fe2O3T.
    logdata : :class:`bool`, :code:`False`
        Whether the data has been log transformed.
    molecular : :class:`bool`, :code:`False`
        Whether to perform a sum of molecular data.

    Notes
    -------
    This won't convert units, so need to start from single set of units.

    Returns
    -------
    :class:`pandas.Series`
        Series with cation aggregated.
    """
    # get the elemental sum
    subsum = elemental_sum(
        df, to, total_suffix=total_suffix, logdata=logdata, molecular=molecular
    )
    cation = subsum.name
    species = simple_oxides(cation)
    species += [i + total_suffix for i in species]
    species = [i for i in species if i in df.columns]
    _df = df.copy()
    if isinstance(to, str):
        toform = remove_suffix(to, suffix=total_suffix)
        drop = [i for i in species if str(i) != to]
        targetnames = [to]
        props = [1.0]
        coeff = [oxide_conversion(cation, toform, molecular=molecular)(1)]
    elif isinstance(to, (pt.core.Element, pt.formulas.Formula)):
        to = str(to)
        drop = [i for i in species if str(i) != to]
        targetnames = [to]
        props = [1.0]
        coeff = [oxide_conversion(cation, to, molecular=molecular)(1)]
    elif isinstance(to, dict):
        targets = list(to.items())
        targetnames = [str(t[0]) for t in targets]
        props = close(np.array([t[1] for t in targets]).astype(np.float))
        coeff = [
            oxide_conversion(cation, t, molecular=molecular)(p)
            for t, p in zip(targetnames, props)
        ]
        drop = [i for i in species if str(i) not in targetnames]
    else:
        raise NotImplementedError("Not yet implemented for tuples, lists, arrays etc.")
    logger.debug(
        "Transforming {} to: {}".format(
            cation,
            {k: "{:2.1f}%".format(v * 100) for (k, v) in zip(targetnames, props)},
        )
    )
    if drop:
        logger.debug("Dropping redundant columns: {}".format(", ".join(drop)))
        df = df.drop(columns=drop)

    for t in targetnames:
        if t not in _df:
            _df[t] = np.nan  # avoid missing column errors

    _df.loc[:, targetnames] = (
        subsum.values[:, np.newaxis] @ np.array(coeff)[np.newaxis, :]
    )

    if logdata:
        logger.debug("Log-transforming {} Data.".format(cation))
        _df.loc[:, targetnames] = _df.loc[:, targetnames].applymap(np.log)

    df[targetnames] = _df.loc[:, targetnames]
    if renorm:
        return renormalise(df)
    else:
        return df


def recalculate_Fe(
    df: pd.DataFrame,
    to="FeOT",
    renorm=False,
    total_suffix="T",
    logdata=False,
    molecular=False,
):
    """
    Recalculates abundances of iron, and normalises a dataframe to contain  either
    a single species, or multiple species in certain proportions.

    Parameters
    -----------
    df : :class:`pandas.DataFrame`
        Dataframe to recalcuate iron.
    to : :class:`str` | :class:`~periodictable.core.Element` | :class:`~periodictable.formulas.Formula`  | :class:`dict`
        Component(s) to convert to.

        If one component is specified, all iron will be
        converted to the target species.

        If more than one component is specified with proportions in a dictionary
        (e.g. :code:`{'FeO': 0.9, 'Fe2O3': 0.1}`), the components will be split as a
        fraction of Fe.
    renorm : :class:`bool`, :code:`False`
        Whether to renormalise the dataframe after recalculation.
    total_suffix : :class:`str`, 'T'
        Suffix of 'total' variables. E.g. 'T' for FeOT, Fe2O3T.
    logdata : :class:`bool`, :code:`False`
        Whether the data has been log transformed.
    molecular : :class:`bool`, :code:`False`
        Flag that data is in molecular units, rather than weight units.

    Returns
    -------
    :class:`pandas.DataFrame`
        Transformed dataframe.
    """
    return aggregate_element(
        df,
        to=to,
        renorm=renorm,
        total_suffix=total_suffix,
        logdata=logdata,
        molecular=molecular,
    )


def add_ratio(
    df: pd.DataFrame, ratio: str, alias: str = None, norm_to=None, molecular=False
):
    """
    Add a ratio of components A and B, given in the form of string 'A/B'.
    Returned series be assigned an alias name.

    Parameters
    -----------
    df : :class:`pandas.DataFrame`
        Dataframe to append ratio to.
    ratio : :class:`str`
        String decription of ratio in the form A/B[_n].
    alias : :class:`str`
        Alternate name for ratio to be used as column name.
    norm_to : :class:`str` | :class:`pyrolite.geochem.norm.RefComp`, `None`
        Reference composition to normalise to.
    molecular : :class:`bool`, :code:`False`
        Flag that data is in molecular units, rather than weight units.

    Returns
    -------
    :class:`pandas.DataFrame`
        Dataframe with ratio appended.

    Todo
    ------

        * Use elemental sum from reference compositions
        * Use sympy-like functionality to accept arbitrary input for calculation

            e.g. :code:`"MgNo = Mg / (Mg + Fe)"`

    See Also
    --------
    :func:`~pyrolite.geochem.transform.add_MgNo`
    """
    num, den = ratio.split("/")
    _to_norm = False
    if den.lower().endswith("_n"):
        den = titlecase(den.lower().replace("_n", ""))
        _to_norm = True

    if _to_norm or (norm_to is not None):  # if molecular, this will need to change
        if isinstance(norm_to, str):
            norm = get_reference_composition(norm_to)
            num_n, den_n = norm[num].value, norm[den].value
        elif isinstance(norm_to, RefComp):
            num_n, den_n = norm_to[num].value, norm_to[den].value
        elif iscollection(norm_to):  # list, iterable, pd.Index etc
            num_n, den_n = norm_to
        else:
            norm = get_reference_composition("Chondrite_PON")
            num_n, den_n = norm[num].value, norm[den].value

    name = [ratio if ((not alias) or (alias is None)) else alias][0]
    logger.debug("Adding Ratio: {}".format(name))
    numsum, densum = (
        elemental_sum(df, num, to=num, molecular=molecular),
        elemental_sum(df, den, to=den, molecular=molecular),
    )
    ratio = numsum / densum
    ratio[~np.isfinite(ratio.values)] = np.nan  # avoid inf
    df[name] = ratio
    return df


def add_MgNo(
    df: pd.DataFrame,
    molecular=False,
    use_total_approx=False,
    approx_Fe203_frac=0.1,
    name="Mg#",
):
    """
    Append the magnesium number to a dataframe.

    Parameters
    ----------
    df : :class:`pandas.DataFrame`
        Input dataframe.
    molecular : :class:`bool`, :code:`False`
        Whether the input data is molecular.
    use_total_approx : :class:`bool`, :code:`False`
        Whether to use an approximate calculation using total iron rather than just FeO.
    approx_Fe203_frac : :class:`float`
        Fraction of iron which is oxidised, used in approximation mentioned above.
    name : :class:`str`
        Name to use for the Mg Number column.

    Returns
    -------
    :class:`pandas.DataFrame`
        Dataframe with ratio appended.

    See Also
    --------
    :func:`~pyrolite.geochem.transform.add_ratio`
    """
    logger.debug("Adding Mg#")
    mg = elemental_sum(df, "Mg", molecular=molecular)
    if use_total_approx:
        speciation = {"FeO": 1.0 - approx_Fe203_frac, "Fe2O3": approx_Fe203_frac}
        fe = aggregate_element(df, "Fe", to=speciation, molecular=molecular).FeO
    else:
        filter = [i for i in df.columns if "Fe2O3" not in i]  # exclude ferric iron
        fe = elemental_sum(df.loc[:, filter], "Fe", molecular=molecular)
    if not molecular:  # convert these outputs to molecular, unless already so
        mg, fe = (
            to_molecular(mg.to_frame(), renorm=False),
            to_molecular(fe.to_frame(), renorm=False),
        )

    mgnos = mg.values / (mg.values + fe.values)
    if mgnos.size:  # to cope with empty arrays
        df[name] = mgnos
    else:
        df[name] = None
    return df


@update_docstring_references
def lambda_lnREE(
    df,
    norm_to="Chondrite_PON",
    exclude=["Pm", "Eu"],
    params=None,
    degree=4,
    append=[],
    scale="ppm",
    **kwargs
):
    """
    Calculates orthogonal polynomial coefficients (lambdas) for a given set of REE data,
    normalised to a specific composition [#ref_1]_. Lambda factors are given for the
    radii vs. ln(REE/NORM) polynomical combination.

    Parameters
    ------------
    df : :class:`pandas.DataFrame`
        Dataframe to calculate lambda coefficients for.
    norm_to : :class:`str` | :class:`~pyrolite.geochem.norm.RefComp` | :class:`numpy.ndarray`
        Which reservoir to normalise REE data to (defaults to :code:`"Chondrite_PON"`).
    exclude : :class:`list`, :code:`["Pm", "Eu"]`
        Which REE elements to exclude from the fit. May wish to include Ce for minerals
        in which Ce anomalies are common.
    params : :class:`list`, :code:`None`
        Set of predetermined orthagonal polynomial parameters.
    degree : :class:`int`, 5
        Maximum degree polynomial fit component to include.
    append : :class:`list`, :code:`None`
        Whether to append lambda function (i.e. :code:`["function"]`).
    scale : :class:`str`
        Current units for the REE data, used to scale the reference dataset.

    Todo
    -----
        * Operate only on valid rows.
        * Add residuals, Eu, Ce anomalies as options to `append`.
        * Pre-build orthagonal parameters for REE combinations for calculation speed?

    References
    -----------
    .. [#ref_1] O’Neill HSC (2016) The Smoothness and Shapes of Chondrite-normalized
           Rare Earth Element Patterns in Basalts. J Petrology 57:1463–1508.
           doi: `10.1093/petrology/egw047 <https://dx.doi.org/10.1093/petrology/egw047>`__


    See Also
    ---------
    :func:`~pyrolite.geochem.ind.get_ionic_radii`
    :func:`~pyrolite.util.math.lambdas`
    :func:`~pyrolite.util.math.OP_constants`
    :func:`~pyrolite.plot.REE_radii_plot`
    :func:`~pyrolite.geochem.norm.ReferenceCompositions`
    """
    non_null_cols = df.columns[~df.isnull().all(axis=0)]
    ree = [
        i
        for i in REE()
        if i in df.columns
        and (not str(i) in exclude)
        and (str(i) in non_null_cols or i in non_null_cols)
    ]  # no promethium
    radii = np.array(get_ionic_radii(ree, coordination=8, charge=3))

    if params is None:
        params = OP_constants(radii, degree=degree)
    else:
        degree = len(params)

    null_in_row = pd.isnull(df.loc[:, ree]).any(axis=1)
    norm_df = df.loc[~null_in_row, ree].copy()  # initialize normdf

    labels = [chr(955) + str(d) for d in range(degree)]

    if norm_to is not None:  # None = already normalised data
        if isinstance(norm_to, str):
            norm = get_reference_composition(norm_to)
            norm.set_units(scale)
            norm_abund = np.array([norm[str(el)].value for el in ree])
        elif isinstance(norm_to, RefComp):
            norm = norm_to
            norm.set_units(scale)
            norm_abund = np.array([norm[str(el)].value for el in ree])
        else:  # list, iterable, pd.Index etc
            norm_abund = np.array(norm_to)
            assert len(norm_abund) == len(ree)

        norm_df.loc[:, ree] = np.divide(norm_df.loc[:, ree].values, norm_abund)

    norm_df.loc[(norm_df <= 0.0).any(axis=1), :] = np.nan  # remove zero or below
    norm_df.loc[:, ree] = norm_df.loc[:, ree].applymap(np.log)

    lambdadf = pd.DataFrame(index=df.index, columns=labels)
    lambda_partial = functools.partial(
        lambdas, xs=radii, params=params, degree=degree, **kwargs
    )  # pass kwargs to lambdas
    # apply along rows
    logger.debug("lambda-fitting")
    lambdadf.loc[~null_in_row, labels] = np.apply_along_axis(
        lambda_partial, 1, norm_df.values
    )
    lambdadf.loc[(lambdadf == 0.0).all(axis=1), :] = np.nan
    if append is not None:
        if "function" in append:
            # append the smooth f(radii) function to the dataframe
            func_partial = functools.partial(
                lambda_poly_func, pxs=radii, params=params, degree=degree
            )
            lambdadf["lambda_poly_func"] = np.apply_along_axis(
                func_partial, 1, lambdadf.values
            )

    lambdadf = lambdadf.apply(pd.to_numeric, errors="coerce")
    assert lambdadf.index.size == df.index.size
    return lambdadf


def convert_chemistry(input_df, to=[], logdata=False, renorm=False, molecular=False):
    """
    Attempts to convert a dataframe with one set of components to another.

    Parameters
    -----------
    input_df : :class:`pandas.DataFrame`
        Dataframe to convert.
    to : :class:`list`
        Set of columns to try to extract from the dataframe.

        Can also include a dictionary for iron speciation. See :func:`recalculate_Fe`.
    logdata : :class:`bool`, :code:`False`
        Whether chemical data has been log transformed. Necessary for aggregation
        functions.
    renorm : :class:`bool`, :code:`False`
        Whether to renormalise the data after transformation.
    molecular : :class:`bool`, :code:`False`
        Flag that data is in molecular units, rather than weight units.

    Returns
    --------
    :class:`pandas.DataFrame`
        Dataframe with converted chemistry.

    Todo
    ------
        * Check for conflicts between oxides and elements
        * Aggregator for ratios
        * Implement generalised redox transformation.
        * Add check for dicitonary components (e.g. Fe) in tests
    """
    df = input_df.copy()
    oxides = __common_oxides__
    elements = __common_elements__
    compositional_components = oxides | elements
    # multi-component dictionaries which are not elements/oxides/ratios
    coupled_sets = [
        i for i in to if not isinstance(i, (str, pt.core.Element, pt.formulas.Formula))
    ]
    logger.debug(
        "Found coupled sets: {}".format(", ".join([str(set(s)) for s in coupled_sets]))
    )
    # check that all sets in coupled_sets have the same cation
    coupled_components = [k for s in coupled_sets for k in s.keys()]
    # need to get the additional things from here
    present_comp = [
        i for i in df.columns if i in compositional_components
    ] + coupled_components
    noncomp = [i for i in df.columns if (i not in present_comp)]
    new_ratios = [i for i in to if "/" in i and i not in df.columns]
    get_comp = [i for i in to if i not in coupled_sets + noncomp + new_ratios]
    agg_present, get_notpresent = (
        [i for i in get_comp if i in present_comp],
        [i for i in get_comp if i not in present_comp],
    )
    # remove iron components from main getter, we'll deal with them separately
    # fe_components = ["Fe", "FeO", "Fe2O3", "Fe2O3T", "FeOT"]
    current_fe = [i for i in present_comp if "Fe" in str(i)]
    get_fe = [i for i in get_notpresent if "Fe" in str(i)]

    agg_present = list(set(agg_present) - set(current_fe))
    get_notpresent = list(set(get_notpresent) - set(get_fe))

    # Aggregate the columns which are otherwise OK, then get new columns
    for item in agg_present + get_notpresent:
        df = aggregate_element(df, to=item, logdata=logdata, molecular=molecular)

    # --- Try to get the new columns - iron redox section ------------------------------
    # check if there's a multicomponent speciation problem
    logger.debug("Checking Iron Redox")
    c_fe_str = ", ".join(current_fe)
    # check if any of the coupled_sets dictionaries correspond to iron
    coupled_fe = [s for s in coupled_sets if all(["Fe" in k for k in s])]
    if coupled_fe:
        get_fe = coupled_fe

    if len(get_fe) > 1:
        raise NotImplementedError("Need to specify speciation for >1 Fe components.")

    if get_fe:
        get_fe = get_fe[0]
        logger.debug("Transforming {} to {}.".format(c_fe_str, get_fe))
        df = recalculate_Fe(
            df, to=get_fe, renorm=False, logdata=logdata, molecular=molecular
        )

    # Try to get some ratios -----------------------------------------------------------
    if new_ratios:
        logger.debug("Adding Requested Ratios: {}".format(", ".join(new_ratios)))
        for r in new_ratios:
            df = add_ratio(df, r, molecular=molecular)
            # df = add_ratio(df, r)

    # Last Minute Checks ---------------------------------------------------------------
    remaining = [i for i in get_comp if i not in df.columns]
    assert not len(remaining), "Columns not attained: {}".format(", ".join(remaining))
    output_columns = noncomp + get_comp + coupled_components + new_ratios
    present_comp = [i for i in df.columns if i in compositional_components]
    if renorm:
        logger.debug("Recalculation Done, Renormalising compositional components.")
        df.loc[:, present_comp] = renormalise(df.loc[:, present_comp])
        return df.loc[:, output_columns]
    else:
        logger.debug("Recalculation Done. Data not renormalised.")
        return df.loc[:, output_columns]
