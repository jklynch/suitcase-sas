from collections import Mapping, Sequence
import copy
import json
import logging
import re

import h5py
import numpy as np


def _copy_nexus_md_to_nexus_h5(nexus_md, h5_group_or_dataset):
    """
    Read a metadata dictionary with nexus-ish keys and create a corresponding nexus structure in an H5 file.

    Allowed structures:
        a group with _attributes:
            "entry" : {
                "NXAttributes": {"NX_Class": "NXEntry", "default": "data"}
            }
        will look like
            .../
                <group entry>
                    <attr "NX_Class": "NXEntry">
                    <attr "default: "data">

        a group with a dataset:
            "entry": {
                "_attributes": {"NX_Class": "NXEntry", "default": "data"},
                "program_name": "EPICS areaDetector",
            }
        will look like
            .../
                <group "entry">
                    <attr "NX_Class": "NXEntry">
                    <attr "default: "data">
                    <dataset "program_name": "EPICS areaDetector">

        a dataset with attributes:
            "entry": {
                "_attributes": {"NX_Class": "NXEntry", "default": "data"},
                "program_name": {
                    "_attributes": {
                        "NDAttrDescription": "program name",
                        "NDAttrName": "ProgramName",
                        "NDAttrSource": "91dcLAX:GUPNumber",
                        "NDAttrSourceType": "NDAttrSourceEPICSPV"
                    },
                    "_dataset": "EPICS areaDetector",
                }
            }
        will look like
            .../
                <group "entry">
                    <attr "NX_Class": "NXEntry">
                    <attr "default: "data">
                    <dataset "program_name": "EPICS areaDetector">
                        <attr "NDAttrDescription": "program name">
                        <attr "NDAttrName: "ProgramName">
                        <attr "NDAttrSource": "91dcLAX:GUPNumber">
                        <attr "NDAttrSourceType": "NDAttrSourceEPICSPV">

        a group with a link to part of the bluesky structure
            "entry": {
                "_attributes": {"NX_Class": "NXEntry", "default": "data"},
                "GUPNumber": "#bluesky/start/gup_number"
            }
        will look like
            .../
                <group "entry">
                    <attr "NX_Class": "NXEntry">
                    <attr "default: "data">
                    <link "GUPNumber" to <dataset /bluesky/start/gup_number>>

        a group with a link with attributes to part of the bluesky structure
        note: the "NDAttr..."s are not NeXus
            "entry": {
                "_attributes": {"NX_Class": "NXEntry", "default": "data"},
                "GUPNumber": {
                    "_attributes": {
                        "NDAttrDescription": "GUP proposal number",
                        "NDAttrName": "GUPNumber",
                        "NDAttrSource": "91dcLAX:GUPNumber",
                        "NDAttrSourceType": "NDAttrSourceEPICSPV"
                    },
                    "_link": "#bluesky/start/gup_number"
            }
        will look like
            .../
                <group "entry">
                    <attr "NX_Class": "NXEntry">
                    <attr "default: "data">
                    <link "GUPNumber" to <dataset /bluesky/start/gup_number>>
                        <attr "NDAttrDescription": "GUP proposal number">
                        <attr "NDAttrName": "GUPNumber">
                        <attr "NDAttrSource": "91dcLAX:GUPNumber">
                        <attr "NDAttrSourceType": "NDAttrSourceEPICSPV">

        a group with subgroups:
            "entry": {
                "_attributes": {"NX_Class": "NXEntry", "default": "data"}
                "instrument": {
                    "_attributes": {"NX_Class": "NXInstrument",},
                    "name_1": "#bluesky/start/beamline_id",
                    "name_2": {
                        "_attributes": {"NX_This": "NXThat"},
                        "_link": "#bluesky/start/beamline_id",
                    },
            }


    For example:
       "entry": {
            "_attributes": {"NX_Class": "NXEntry", "default": "data"},
            "GUPNumber": {
                "_attributes": {
                    "NDAttrDescription": "GUP proposal number",
                    "NDAttrName": "GUPNumber",
                    "NDAttrSource": "91dcLAX:GUPNumber",
                    "NDAttrSourceType": "NDAttrSourceEPICSPV"
                },
                "_link": "#bluesky/start/gup_number"
            },
            "title": {
                "_attributes": {
                    "NDAttrDescription": "sample name",
                    "NDAttrName": "SampleTitle",
                    "NDAttrSource": "91dcLAX:sampleTitle",
                    "NDAttrSourceType": "NDAttrSourceEPICSPV"
                },
                "_link": "#bluesky/start/gup_number"
            },
            "program_name": "EPICS areaDetector",
            "instrument": {
                "_attributes": {"NX_Class": "NXInstrument",},
                "name_1": "#bluesky/start/beamline_id",
                "name_2": {
                    "_attributes": {"NX_This": "NXThat"},
                    "_link": "#bluesky/start/beamline_id",
                },
                "aperture": {
                    "_attributes": {"NX_Class": "NXAperture",},
                    "vcenter": 1.0,
                    "vsize": 2.0,
                    "description": "USAXSslit",
                },
            },
        },
    },

    Parameters
    ----------
    nexus_md: dict-like


    """
    for nexus_key, nexus_value in nexus_md.items():
        if nexus_key in ("_data", "_link"):
            # this key/value has already been processed
            continue
        elif nexus_key == "_attributes":
            for attr_name, attr_value in nexus_value.items():
                h5_group_or_dataset.attrs[attr_name] = attr_value
        elif isinstance(nexus_value, Mapping):
            # we arrive here in a case such as:
            #   "program_name": {
            #      "_attributes": {"attr_1": "abc", "attr_2": "def"},
            #      "_link": "#bluesky/start/program_name"
            #   }
            # where nexus_key is "program_name" and
            # nexus_value is the associated dictionary
            if "_link" in nexus_value:
                h5_group_or_dataset[nexus_key] = _get_h5_group_or_dataset(
                    bluesky_document_path=_parse_bluesky_document_path(
                        nexus_value["_link"]
                    ),
                    h5_file=h5_group_or_dataset.file,
                )
                _copy_nexus_md_to_nexus_h5(
                    nexus_md=nexus_value,
                    h5_group_or_dataset=h5_group_or_dataset[nexus_key],
                )
            elif "_data" in nexus_value:
                # we arrive here in a case such as:
                #   "program_name": {
                #      "_attributes": {"attr_1": "abc", "attr_2": "def"},
                #      "_data": "the name of the program"
                #   }
                # where nexus_key is "program_name" and
                # nexus_value is the associated dictionary
                h5_group_or_dataset.create_dataset(
                    name=nexus_key, data=nexus_value["_data"]
                )
                _copy_nexus_md_to_nexus_h5(
                    nexus_md=nexus_value,
                    h5_group_or_dataset=h5_group_or_dataset[nexus_key],
                )
            else:
                # otherwise create a group
                _copy_nexus_md_to_nexus_h5(
                    nexus_md=nexus_value,
                    h5_group_or_dataset=h5_group_or_dataset.create_group(nexus_key),
                )
        elif isinstance(nexus_value, str) and nexus_value.startswith("#bluesky"):
            # create a link
            bluesky_document_path = _parse_bluesky_document_path(nexus_value)
            h5_group_or_dataset[nexus_key] = _get_h5_group_or_dataset(
                bluesky_document_path, h5_group_or_dataset.file
            )
        else:
            h5_group_or_dataset.create_dataset(name=nexus_key, data=nexus_value)


_bluesky_doc_query_re = re.compile(
    r"^#bluesky/"
    r"(?P<doc>(start|stop|desc/(?P<stream>\w+)))"
    r"(?P<all_keys>(/\w+)*)"
    r"(@(?P<attribute>\w+))?"
)


def _parse_bluesky_document_path(bluesky_document_path):
    """
    regex101.com

    #bluesky/start/blah/bleh@blih :
        doc:       start
        keys:      ("blah", "bleh")
        attribute: blih

    #bluesky/desc/primary/blah/bleh
        doc:    desc
        stream: primary
        keys:   /blah/bleh

    """
    m = _bluesky_doc_query_re.match(bluesky_document_path)
    if m is None:
        raise Exception(f"failed to parse '{bluesky_document_path}'")
    else:
        path_info = copy.copy(m.groupdict())
        if path_info["doc"].startswith("desc"):
            # path_info["doc"] is "desc/stream_name"
            # but I want just "desc" so split off "/stream_name
            path_info["doc"] = path_info["doc"].split("/")[0]
        # path_info["all_keys"] is something like "/abc/def"
        # but I want a tuple like ("abc", "def") so split on "/"
        # the first element of the split list is an empty string
        # leave it out with [1:]
        path_info["keys"] = tuple(path_info["all_keys"].split("/"))[1:]

    return path_info


def _get_h5_group_or_dataset(bluesky_document_path, h5_file):
    # look up the h5 group corresponding to the bluesky document path
    doc = bluesky_document_path["doc"]
    h5_target_group = h5_file["bluesky"][doc]
    for key in bluesky_document_path["keys"]:
        h5_target_group = h5_target_group[key]
    return h5_target_group


def _copy_metadata_to_h5_attrs(a_mapping, h5_group):
    """
    Recursively reproduce a python "mapping" (typically a dict)
    as h5 nested groups and attributes.
    """
    for key, value in a_mapping.items():
        if isinstance(value, Mapping):
            # found a dict-like value
            # create a new h5 group for it
            # and recursively copy its keys and values to h5 groups and attributes
            _copy_metadata_to_h5_attrs(
                a_mapping=value, h5_group=h5_group.create_group(key)
            )
        else:
            # a special case
            if value is None:
                value = "None"

            try:
                # this is where an h5 attribute is assigned
                h5_group.attrs[key] = value
            except TypeError:
                # `value` is too complicated to be a h5 attribute
                # an example of a key-value pair that will cause TypeError is
                #   {'dimensions': [[['time'], 'primary']]}
                # instead we will store it as JSON
                h5_group.attrs[key] = json.dumps(value)


def _copy_metadata_to_h5_datasets(a_mapping, h5_group):
    """
    Recursively reproduce a python "mapping" (typically a dict)
    as h5 nested groups and datasets. This function is intended
    to be used when h5 attributes are not desirable, for example
    if we want to create h5 links to the resulting datasets.
    """
    log = logging.Logger("suitcase.nxsas", level="DEBUG")
    for key, value in a_mapping.items():
        if isinstance(value, Mapping):
            # found a dict-like value
            # create a new h5 group for it
            # and recursively copy its keys and values to h5 groups and datasets
            group = h5_group.create_group(key)
            log.debug("created h5 group %s", group)
            _copy_metadata_to_h5_datasets(a_mapping=value, h5_group=group)
        else:
            # a special case
            if value is None:
                value = "None"
            elif value == b"\x00":
                # for example:
                # "en_monoen_grating_clr_enc_lss": {
                #     "source": "PV:XF:07ID1-OP{Mono:PGM1-Ax:GrtP}Mtr_ENC_LSS_CLR_CMD.PROC",
                #     "dtype": "integer",
                #     "shape": [],
                #     "units": "",
                #     "lower_ctrl_limit": b"\x00",
                #     "upper_ctrl_limit": b"\x00",
                #     "object_name": "en",
                #
                # },
                # will cause a ValueError: VLEN strings do not support embedded NULLs
                value = ""

            # this is where an h5 dataset is assigned
            # string datasets are special because they must be explicitly
            # converted to a numpy array with dtype=h5py.string_dtype()
            try:
                # check for str or Sequence of str
                # use Sequence to handle list and tuple
                if isinstance(value, str) or (
                    isinstance(value, Sequence)
                    and all([isinstance(x, str) for x in value])
                ):
                    d = h5_group.create_dataset(
                        name=key, data=np.array(value, dtype=h5py.string_dtype())
                    )
                else:
                    d = h5_group.create_dataset(name=key, data=value)
            except TypeError as err:
                # TypeError occurs if the 'value' is too complex for create_dataset.
                # Handle this exception by JSON-encoding `value`.
                log.info(
                    "handling exception '%s' by JSON-encoding value '%s' for key '%s'",
                    err,
                    value,
                    key,
                )
                d = h5_group.create_dataset(
                    name=key,
                    data=np.array(json.dumps(value), dtype=h5py.string_dtype()),
                )
            except BaseException as ex:
                # all other exceptions will be logged and allowed to propagate
                log.error(
                    "failed to create dataset in group '%s' for key '%s' with value '%s'",
                    h5_group,
                    key,
                    value,
                )
                log.exception(ex)
                raise ex

            log.debug("created dataset %s", d)
