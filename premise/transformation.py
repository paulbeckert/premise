"""
transformation.py contains the base class TransformationBase, used by other modules.
It provides basic methods usually used for electricity, cement, steel sectors transformation
on the wurst database.
"""

import csv
import uuid
from datetime import date
from itertools import product

import numpy as np
import wurst
from wurst import searching as ws
from wurst import transformations as wt

from . import DATA_DIR
from .activity_maps import InventorySet
from .geomap import Geomap
from .utils import (
    get_fuel_co2_emission_factors,
    get_lower_heating_values,
    relink_technosphere_exchanges,
)


def get_suppliers_of_a_region(database, locations, names, reference_product, unit):
    """
    Return a list of datasets, for which the location, name, reference production and unit correspond
    to the region and name given, respectively.

    :param database: database to search
    :type database: list of dictionaries
    :param locations: list of locations
    :type locations: list
    :param names: names of datasets
    :type names: list
    :param unit: unit of dataset
    :type unit: str
    :param reference_product: reference product of dataset
    :type reference_product: str
    :return: list of wurst datasets
    :rtype: list
    """

    return ws.get_many(
        database,
        *[
            ws.either(*[ws.contains("name", supplier) for supplier in names]),
            ws.either(*[ws.equals("location", loc) for loc in locations]),
            ws.contains("reference product", reference_product),
            ws.equals("unit", unit),
        ],
    )


def get_shares_from_production_volume(ds_list):
    """
    Return shares of supply of each datasets in `ds_list` based on respective production volumes
    :param ds_list: list of datasets
    :type ds_list: list
    :return: dictionary with (dataset name, dataset location, ref prod, unit) as keys, shares as values. Shares total 1.
    :rtype: dict
    """

    if not isinstance(ds_list, list):
        ds_list = [ds_list]

    dict_act = {}
    total_production_volume = 0
    for act in ds_list:
        for exc in ws.production(act):
            # even if non-existent, we set a minimum value of 1e-9
            # because if not, we risk dividing by zero!!!
            production_volume = max(float(exc.get("production volume", 1e-9)), 1e-9)

            dict_act[
                (act["name"], act["location"], act["reference product"], act["unit"],)
            ] = production_volume
            total_production_volume += production_volume

    for dataset in dict_act:
        dict_act[dataset] /= total_production_volume

    return dict_act


def get_tuples_from_database(database):
    """
    Return a list of tuples (name, reference product, location)
    for each dataset in database.
    :param database:
    :return: a list of tuples
    :rtype: list
    """
    return [
        (dataset["name"], dataset["reference product"], dataset["location"])
        for dataset in database
    ]


def remove_exchanges(datasets_dict, list_exc):
    """
    Returns the same `datasets_dict`, where the list of exchanges in these datasets
    has been filtered out: unwanted exchanges has been removed.
    :param datasets_dict: a dictionary with IAM region as key, dataset as value
    :param list_exc: list of names (e.g., ["coal", "lignite"]) which are checked against exchanges' names in the dataset
    :return: returns `datasets_dict` without the exchanges whose names check with `list_exc`
    :rtype: dict
    """
    keep = lambda x: {
        key: value
        for key, value in x.items()
        if not any(ele in x.get("product", []) for ele in list_exc)
    }

    for region in datasets_dict:
        datasets_dict[region]["exchanges"] = [
            keep(exc) for exc in datasets_dict[region]["exchanges"]
        ]

    return datasets_dict


class BaseTransformation:
    """
    Base transformation class.
    """

    def __init__(self, database, iam_data, model, pathway, year):
        self.database = database
        self.iam_data = iam_data
        self.model = model
        self.regions = iam_data.regions
        self.geo = Geomap(model=model, current_regions=self.regions)
        self.scenario = pathway
        self.year = year
        self.fuels_lhv = get_lower_heating_values()
        mapping = InventorySet(self.database)
        self.emissions_map = mapping.get_remind_to_ecoinvent_emissions()
        self.fuel_map = mapping.generate_fuel_map()
        self.material_map = mapping.generate_material_map()
        self.fuels_co2 = get_fuel_co2_emission_factors()
        self.list_datasets = get_tuples_from_database(self.database)
        self.ecoinvent_to_iam_loc = {
            loc: self.geo.ecoinvent_to_iam_location(loc)
            for loc in self.get_ecoinvent_locs()
        }
        self.cache = {}

    def get_ecoinvent_locs(self):
        """
        Rerun a list of unique locations in ecoinvent

        :return: list of location
        :rtype: list
        """

        return list(set([a["location"] for a in self.database]))

    def update_ecoinvent_efficiency_parameter(self, dataset, old_ei_eff, new_eff):
        """
        Update the old efficiency value in the ecoinvent dataset by the newly calculated one.
        :param dataset: dataset
        :type dataset: dict
        :param scaling_factor: scaling factor (new efficiency / old efficiency)
        :type scaling_factor: float
        """
        parameters = dataset["parameters"]
        possibles = ["efficiency", "efficiency_oil_country", "efficiency_electrical"]

        if any(i in dataset for i in possibles):
            for key in possibles:
                if key in parameters:
                    dataset["parameters"][key] = new_eff
        else:
            dataset["parameters"]["efficiency"] = new_eff

        iam_region = self.ecoinvent_to_iam_loc[dataset["location"]]

        new_txt = (
            f" 'premise' has modified the efficiency of this dataset, from an original "
            f"{int(old_ei_eff * 100)}% to {int(new_eff * 100)}%, according to IAM model {self.model.upper()}, scenario {self.scenario} "
            f"for the region {iam_region}."
        )

        if "comment" in dataset:
            dataset["comment"] += new_txt
        else:
            dataset["comment"] = new_txt

    def find_fuel_efficiency(self, dataset, fuel_filters, energy_out):
        """
        This method calculates the efficiency value set initially, in case it is not specified in the parameter
        field of the dataset. In Carma datasets, fuel inputs are expressed in megajoules instead of kilograms.

        :param dataset: a wurst dataset of an electricity-producing technology
        :param fuel_filters: wurst filter to filter fuel input exchanges
        :param energy_out: the amount of energy expect as output, in MJ
        :return: the efficiency value set initially
        """

        def calculate_input_energy(fuel_name, fuel_amount, fuel_unit):
            """
            Returns the amount of energy entering the conversion process, in MJ
            :param fuel_name: name of the liquid, gaseous or solid fuel
            :param fuel_amount: amount of fuel input
            :param fuel_unit: unit of fuel
            :return: amount of fuel energy, in MJ
            """

            # if fuel input other than MJ
            if fuel_unit in ["kilogram", "cubic meter", "kilowatt hour"]:
                lhv = [
                    self.fuels_lhv[k] for k in self.fuels_lhv if k in fuel_name.lower()
                ][0]
                return float(lhv) * fuel_amount

            # if already in MJ
            return fuel_amount

        not_allowed = ["thermal"]
        key = []
        if "parameters" in dataset:
            key = list(
                key
                for key in dataset["parameters"]
                if "efficiency" in key and not any(item in key for item in not_allowed)
            )

        if len(key) > 0:
            return dataset["parameters"][key[0]]

        energy_input = np.sum(
            np.sum(
                np.asarray(
                    [
                        calculate_input_energy(exc["name"], exc["amount"], exc["unit"])
                        for exc in dataset["exchanges"]
                        if exc["name"] in fuel_filters and exc["type"] == "technosphere"
                    ]
                )
            )
        )

        current_efficiency = float(energy_out) / energy_input

        if current_efficiency in (np.nan, np.inf):
            current_efficiency = 1

        if "parameters" in dataset:
            dataset["parameters"]["efficiency"] = current_efficiency
        else:
            dataset["parameters"] = {"efficiency": current_efficiency}

        return current_efficiency

    def get_iam_mapping(self, activity_map, fuels_map, technologies):
        """
        Define filter functions that decide which wurst datasets to modify.
        :param activity_map: a dictionary that contains 'technologies' as keys and activity names as values.
        :param fuels_map: a dictionary that contains 'technologies' as keys and fuel names as values.
        :param technologies: a list of IAM technologies.
        :return: dictionary that contains filters and functions
        :rtype: dict
        """

        return {
            tech: {
                "IAM_eff_func": self.find_iam_efficiency_change,
                "current_eff_func": self.find_fuel_efficiency,
                "technology filters": activity_map[tech],
                "fuel filters": fuels_map[tech],
            }
            for tech in technologies
        }

    def fetch_proxies(self, name, ref_prod, production_variable, relink=False):
        """
        Fetch dataset proxies, given a dataset `name` and `reference product`.
        Store a copy for each IAM region.
        If a fitting ecoinvent location cannot be found for a given IAM region,
        fetch a dataset with a "RoW" location.
        Delete original datasets from the database.

        :param name: name of the datasets to find
        :type name: str
        :param ref_prod: reference product of the datasets to find
        :type ref_prod: str
        :param production_variable: name of variable in IAM data that refers to production volume
        :type production_variable: list or str
        :param relink: if `relink`, exchanges from the datasets will be relinked to
        the most geographically-appropriate providers from the database. This is computer-intensive.
        :type relink: bool
        :return:
        """

        d_map = {
            self.ecoinvent_to_iam_loc[d["location"]]: d["location"]
            for d in ws.get_many(
                self.database,
                ws.equals("name", name),
                ws.contains("reference product", ref_prod),
            )
        }

        d_iam_to_eco = {region: d_map.get(region, "RoW") for region in self.regions}

        d_act = {}

        for region in d_iam_to_eco:

            try:
                dataset = ws.get_one(
                    self.database,
                    ws.equals("name", name),
                    ws.contains("reference product", ref_prod),
                    ws.equals("location", d_iam_to_eco[region]),
                )

            except ws.NoResults:

                # trying with `GLO`
                dataset = ws.get_one(
                    self.database,
                    ws.equals("name", name),
                    ws.contains("reference product", ref_prod),
                    ws.equals("location", "GLO"),
                )

            d_act[region] = wt.copy_to_new_location(dataset, region)
            d_act[region]["code"] = str(uuid.uuid4().hex)

            for exc in ws.production(d_act[region]):
                if "input" in exc:
                    exc.pop("input")

            if "input" in d_act[region]:
                d_act[region].pop("input")

            # Add `production volume` field
            if isinstance(production_variable, str):
                production_variable = [production_variable]

            prod_vol = (
                self.iam_data.production_volumes.sel(
                    region=region, variables=production_variable
                )
                .interp(year=self.year)
                .sum(dim="variables")
                .values.item(0)
            )

            for prod in ws.production(d_act[region]):
                prod["location"] = region
                prod["production volume"] = prod_vol

            if relink:
                d_act[region] = relink_technosphere_exchanges(
                    d_act[region], self.database, self.model
                )

            ds_name = d_act[region]["name"]
            ds_ref_prod = d_act[region]["reference product"]

        deleted_markets = [
            (act["name"], act["reference product"], act["location"])
            for act in self.database
            if (act["name"], act["reference product"]) == (ds_name, ds_ref_prod)
        ]

        with open(
            DATA_DIR
            / f"logs/log deleted datasets {self.model} {self.scenario} {self.year}-{date.today()}.csv",
            "a",
            encoding="utf-8",
        ) as csv_file:
            writer = csv.writer(csv_file, delimiter=";", lineterminator="\n")
            for line in deleted_markets:
                writer.writerow(line)

        # Remove old datasets
        self.database = [
            act
            for act in self.database
            if (act["name"], act["reference product"]) != (ds_name, ds_ref_prod)
        ]

        # Remove deleted datasets from `self.list_datasets`
        self.list_datasets = [
            dataset
            for dataset in self.list_datasets
            if (dataset[0], dataset[1]) != (ds_name, ds_ref_prod)
        ]

        # Add created datasets to `self.list_datasets`
        self.list_datasets.extend(
            [
                (dataset["name"], dataset["reference product"], dataset["location"])
                for dataset in d_act.values()
            ]
        )

        return d_act

    def relink_datasets(self, excludes_datasets, alternative_names=None):
        """
        For a given exchange name, product and unit, change its location to an IAM location,
        to effectively link to the newly built market(s)/activity(ies).

        :param name: dataset name
        :type name: str
        :param ref_product: reference product of the dataset
        :type ref_product: str
        :param unit: unit of the dataset
        :type unit: str
        :param excludes: list of terms that, if contained in the name of an exchange, should be ignored
        :type excludes: list
        :returns: does not return anything. Modifies in place.
        """

        if alternative_names is None:
            alternative_names = []

        # loop through the database
        # ignore datasets which name contains `name`
        for act in ws.get_many(
            self.database, ws.doesnt_contain_any("name", excludes_datasets),
        ):
            # and find exchanges of datasets to relink

            excs_to_relink = [
                exc
                for exc in act["exchanges"]
                if exc["type"] == "technosphere"
                and (exc["name"], exc["product"], exc["location"])
                not in self.list_datasets
            ]

            unique_excs_to_relink = list(
                set(
                    (exc["name"], exc["product"], exc["location"], exc["unit"])
                    for exc in excs_to_relink
                )
            )



            for exc in unique_excs_to_relink:

                try:
                    new_name, new_prod, new_loc, new_unit = self.cache[act["location"]][
                        exc
                    ]

                except KeyError:

                    alternative_names = [exc[0], *alternative_names]
                    alternative_locations = (
                        [act["location"]]
                        if act["location"] in self.regions
                        else [self.ecoinvent_to_iam_loc[act["location"]]]
                    )

                    for alt_name, alt_loc in product(
                        alternative_names, alternative_locations
                    ):

                        if (alt_name, exc[1], alt_loc) in self.list_datasets:
                            new_name, new_prod, new_loc, new_unit = (
                                alt_name,
                                exc[1],
                                alt_loc,
                                exc[-1],
                            )

                            if act["location"] in self.cache:
                                self.cache[act["location"]][exc] = (
                                    alt_name,
                                    exc[1],
                                    alt_loc,
                                    exc[-1],
                                )
                            else:
                                self.cache[act["location"]] = {
                                    exc: (alt_name, exc[1], alt_loc, exc[-1])
                                }
                            break

                # summing up the amounts provided by the unwanted exchanges
                # and remove these unwanted exchanges from the dataset
                amount = sum(
                    e["amount"]
                    for e in excs_to_relink
                    if (e["name"], e["product"], e["location"], e["unit"]) == exc
                )

                act["exchanges"] = [
                    e
                    for e in act["exchanges"]
                    if (e["name"], e.get("product"), e.get("location"), e["unit"])
                    != exc
                ]

                # create a new exchange, with the new provider
                try:
                    new_exc = {
                        "name": new_name,
                        "product": new_prod,
                        "amount": amount,
                        "type": "technosphere",
                        "unit": new_unit,
                        "location": new_loc,
                    }

                    act["exchanges"].append(new_exc)

                except:
                    print(
                        f"No alternative provider found for {exc[0], act['location']}."
                    )

    def get_carbon_capture_rate(self, loc, sector):
        """
        Returns the carbon capture rate as indicated by the IAM
        It is calculated as CO2 captured / (CO2 captured + CO2 emitted)

        :param loc: location of the dataset
        :return: rate of carbon capture
        :param sector: name of the sector to look capture rate for
        :type sector: str or list

        :rtype: float
        """

        if sector in self.iam_data.carbon_capture_rate.variables.values:
            rate = self.iam_data.carbon_capture_rate.sel(
                variables=sector, region=loc,
            ).values
        else:
            rate = 0

        return rate

    def find_gains_emissions_change(self, pollutant, location, sector):
        """
        Return the relative change in emissions for a given pollutant, location and sector.
        :param pollutant: name of pollutant
        :param sector: name of technology/sector
        :param location: location of emitting dataset
        :return: a scaling factor
        :rtype: float
        """

        scaling_factor = self.iam_data.emissions.loc[
            dict(region=location, pollutant=pollutant, sector=sector,)
        ].values.item(0)

        return scaling_factor

    def find_iam_efficiency_change(self, variable, location):
        """
        Return the relative change in efficiency for `variable` in `location`
        relative to 2020.
        :param variable: IAM variable name
        :param location: IAM region
        :return: relative efficiency change (e.g., 1.05)
        :rtype: float
        """

        scaling_factor = self.iam_data.efficiency.sel(
            region=location, variables=variable
        ).values.item(0)

        if scaling_factor in (np.nan, np.inf):
            scaling_factor = 1

        return scaling_factor

    def update_pollutant_emissions(self, dataset, sector):
        """
        Update pollutant emissions based on GAINS data.
        We apply a correction factor equal to the relative change in emissions compared
        to 2020
        :return: Does not return anything. Modified in place.
        """

        # Update biosphere exchanges according to GAINS emission values
        for exc in ws.biosphere(
            dataset, ws.either(*[ws.contains("name", x) for x in self.emissions_map])
        ):

            pollutant = self.emissions_map[exc["name"]]

            scaling_factor = self.find_gains_emissions_change(
                pollutant=pollutant,
                location=self.geo.iam_to_GAINS_region(
                    self.geo.ecoinvent_to_iam_location(dataset["location"])
                ),
                sector=sector,
            )

            if exc["amount"] == 0:
                wurst.rescale_exchange(exc, scaling_factor / 1, remove_uncertainty=True)
            else:
                wurst.rescale_exchange(exc, 1 / scaling_factor)

            exc["comment"] = (
                f"This exchange has been modified based on GAINS projections for "
                f"the {sector} sector by `premise`."
            )

        return dataset
