# Copyright (C) 2010-2013 Claudio Guarnieri.
# Copyright (C) 2014-2016 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import collections
import operator
import datetime
import itertools
import json
import os
import re
import sys
import time
# from lib.cuckoo.common.config import Config
# from lib.cuckoo.common.constants import CUCKOO_ROOT
from math import log

try:
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import seaborn as sns
    import hdbscan      # DBSCAN with noises
    from sklearn import metrics     # score
    from sklearn.cluster import DBSCAN      # core, border, outlier
    from sklearn.manifold import TSNE       # visualize high-dimensional data
    from sklearn.metrics.pairwise import cosine_similarity      # To calculate similarity
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.datasets import make_classification
    from sklearn.model_selection import cross_val_score
    from sklearn import metrics


except ImportError, e:
    print >> sys.stderr, "Some of the packages required by CuckooML are not \
        available."
    print >> sys.stderr, e

def init_cuckooml():
    """Initialise CuckooML analysis with default parameters."""
    cfg = Config("cuckooml")

    # Load reports for clustering
    loader = Loader()
    loader.load_binaries(CUCKOO_ROOT + "/" + cfg.cuckooml.data_directory)

    # Get features dictionaries
    simple_features_dict = loader.get_simple_features()
    features_dict = loader.get_features()
    labels_dict = loader.get_labels()

    # Transform them into proper features
    ml = ML()
    ml.load_simple_features(simple_features_dict)
    ml.load_features(features_dict)
    ml.load_labels(labels_dict)

    simple_features = ml.simple_features
    features_nominal = ml.feature_category(":count:", complement=True)
    features_numerical = ml.feature_category(":count:")

    if cfg.cuckooml.clustering:
        # Select features
        selected_features = []
        sf = [i.strip() for i in cfg.cuckooml.features.split(",")]
        if "simple" in sf:
            selected_features.append(simple_features)
        if "nominal" in sf:
            selected_features.append(features_nominal)
        if "numerical" in sf:
            selected_features.append(features_numerical)

        # Apply filters to selected datasets
        filters = [i.strip() for i in cfg.cuckooml.features_filter.split(",")]

        # Check whether features and filters dimension agrees
        if len(filters) != len(selected_features):
            print >> sys.stderr, "Number of *filters* and *selected_features* \
                does not agree."
            sys.exit(1)

        data = []
        for f, d in itertools.izip(filters, selected_features):
            if f == "log_bin":
                data.append(d.applymap(ml.__log_bin))
            elif f == "filter_dataset":
                data.append(ml.filter_dataset(d))
            elif f == "none":
                data.append(d)

        data = pd.concat(data, axis=1)

        # Parse clustering parameters
        cl_args = \
            [i for i in cfg.cuckooml.clustering_parameters.split(",")]

        # Select clustering algorithm
        cls_alg = cfg.cuckooml.clustering_algorithm.lower()
        if cls_alg == "hdbscan":
            cl_args= [int(i) for i in cl_args]
            ml.cluster_hdbscan(data, *cl_args)
        elif cls_alg == "dbscan":
            cl_args= [int(i) for i in cl_args]
            ml.cluster_dbscan(data, *cl_args)

        # Save clustering fit
        clf = {}
        clf["clustering+noise"] = ml.assess_clustering( \
                                      ml.clustering[cls_alg]["clustering"], \
                                      ml.labels, data, discard_noise=False)
        print "Clustering fit:"
        clf["clustering"] = ml.assess_clustering( \
                                ml.clustering[cls_alg]["clustering"], \
                                ml.labels, data, discard_noise=True)
        pd.DataFrame(clf).to_csv("clustering_fit.csv", encoding="utf-8")

        # Save clustering results to csv
        print "Clustering results saved to `clustering_results.csv`"
        ml.clustering[cls_alg]["clustering"].to_csv("clustering_results.csv", \
                                                    encoding="utf-8")
        print "Cluster label distribution saved to \
               `cluster_label_distribution.csv`"
        ml.clustering_label_distribution(ml.clustering[cls_alg]["clustering"], \
                                         ml.labels, plot=False)\
            .to_csv("cluster_label_distribution.csv", encoding="utf-8")

    # Save abnormal behaviour stats?
    if cfg.cuckooml.abnormal_behaviour:
        print "Abnormal behaviour will be saved to `abnormal_behaviour.csv`"
        ml.detect_abnormal_behaviour(features_numerical, figures=False).to_csv(\
            "abnormal_behaviour.csv", encoding="utf-8")

    if cfg.cuckooml.anomalies_detection:
        ad = pd.DataFrame(ml.clustering[cls_alg]["clustering"]["label"])
        ad.columns = ["cluster"]
        ad_dict = ml.anomaly_detection()
        for i in ad_dict:
            if isinstance(ad_dict[i], list):
                ad[i] = pd.Series([1]*len(ad_dict[i]), index=ad_dict[i])
            elif isinstance(ad_dict[i], dict):
                in_list = []
                for j in ad_dict[i]:
                    in_list += ad_dict[i][j]
                ad[i] = pd.Series([1]*len(in_list), index=in_list)
        print "Saving anomaly detection results as `anomalies.csv`"
        ad.to_csv("anomalies.csv", encoding="utf-8")


    if cfg.cuckooml.compare_new_samples:
        test_location = CUCKOO_ROOT + "/" + cfg.cuckooml.test_directory

        new_sample = None
        if os.path.isdir(test_location):
            new_sample = Loader()
            new_sample.load_binaries(test_location)
        elif os.path.isfile(test_location):
            new_sample = Instance()
            new_sample.load_json(test_location)
            new_sample.label_sample()
            new_sample.extract_features()
            new_sample.extract_basic_features()
        else:
            print >> sys.stderr, "Indicated sample(s) to compare with do(es) \
                not exist"

        # Compare new sample(s)
        if new_sample is not None:
            ml.compare_sample(new_sample).to_csv("test_samples.csv", \
                                                encoding="utf-8")

    if cfg.cuckooml.clustering and cfg.cuckooml.save_clustering_results:
        if cfg.cuckooml.clustering_results_directory:
            ml.save_clustering_results(loader, CUCKOO_ROOT+"/"+cfg.cuckooml.\
                                       clustering_results_directory)
        else:
            ml.save_clustering_results(loader)


class ML(object):
    """Feature formatting and machine learning for Cuckoo analysed binaries.
    All functions marked with asterisk (*) were inspired by code distributed
    with "Back to the Future: Malware Detection with Temporally Consistent
    Labels" by Brad Miller at al."""
    SIMPLE_CATEGORIES = {
        "properties":[
            "has_authenticode",
            "has_pdb",
            "pe_features",
            "packer_upx",
            "has_wmi"
        ],
        "behaviour":[
            "dumped_buffer2",
            "suspicious_process",
            "persistence_autorun",
            "raises_exception",
            "sniffer_winpcap",
            "injection_runpe",
            "dumped_buffer",
            "exec_crash",
            "creates_service",
            "allocates_rwx"
        ],
        "exploration":[
            "recon_fingerprint",
            "antidbg_windows",
            "locates_sniffer"
        ],
        "mutex":[
            "ardamax_mutexes",
            "rat_xtreme_mutexes",
            "bladabindi_mutexes"
        ],
        "networking":[
            "network_bind",
            "networkdyndns_checkip",
            "network_http",
            "network_icmp",
            "recon_checkip",
            "dns_freehosting_domain",
            "dns_tld_pw",
            "dns_tld_ru"
        ],
        "filesystem":[
            "modifies_files",
            "packer_polymorphic",
            "creates_exe",
            "creates_doc"
        ],
        "security":[
            "rat_xtreme",
            "disables_security",
            "trojan_redosru",
            "worm_renocide",
            "antivirus_virustotal"
        ],
        "virtualisation":[
            "antivm_vbox_files",
            "antivm_generic_bios",
            "antivm_vmware_keys",
            "antivm_generic_services",
            "antivm_vmware_files",
            "antivm_sandboxie",
            "antivm_vbox_keys",
            "antivm_generic_scsi",
            "antivm_vmware_in_instruction",
            "antivm_generic_disk",
            "antivm_virtualpc"
        ],
        "sanbox":[
            "antisandbox_unhook",
            "antisandbox_mouse_hook",
            "antisandbox_foregroundwindows",
            "antisandbox_productid",
            "antisandbox_idletime",
            "antisandbox_sleep"
        ],
        "infostealer":[
            "infostealer_browser",
            "infostealer_mail",
            "infostealer_keylogger",
            "infostealer_ftp",
        ],
        "ransomware":[
            "ransomware_files",
            "ransomware_bcdedit"
        ]
    }

    CATEGORIES = {
        "static":{
            ":meta:":[
                "",
                "size",
                "timestamp"
            ],
            ":sign:":[
                "",
                "signed"
            ],
            ":heur:":[
                ""
            ],
            ":pack:":[
                ""
            ],
            ":pef:":[
                "lang:"
            ],
            ":simp:":[
                "",
                "count"
            ]
        },
        "dynamic":{
            ":dimp:":[
                "",
                "proc:",
                "mutex:"
            ],
            ":file:":{
              "touch:":[
                  ""
              ],
                "count:":[
                    "",
                    "all",
                    "read",
                    "written",
                    "deleted",
                    "copied",
                    "renamed",
                    "opened",
                    "exists",
                    "failed"
                ]
            },
            ":net:":[
                ""
            ],
            ":reg:":[
                "",
                "write:",
                "del:"
            ],
            ":win:":[
                ""
            ]
        },
        "counts":{
            ":count:":{
                "lang":[""],
                "simp":[
                    "",
                    ":"
                ],
                "proc":[""],
                "dimp":[""],
                "file:":[
                    "",
                    "all",
                    "read",
                    "written",
                    "deleted",
                    "copied",
                    "renamed",
                    "opened",
                    "exists",
                    "failed"
                ],
                "tcp":[""],
                "udp":[""],
                "dns":[""],
                "http":[""],
                "reg:":[
                    "",
                    "write",
                    "del"
                ],
                "wapi":[""]
            }
        }
    }

    PATTERNS = [r"Armadillo", r"PECompact", r"ASPack", r"ASProtect",
        r"Upack", r"U(PX|px)", r"FSG", r"BobSoft Mini Delphi",
        r"InstallShield 2000", r"InstallShield Custom",
        r"Xtreme\-Protector", r"Crypto\-Lock", r"MoleBox", r"Dev\-C\+\+",
        r"StarForce", r"Wise Installer Stub", r"SVK Protector",
        r"eXPressor", r"EXECryptor", r"N(s|S)Pac(k|K)", r"KByS",
        r"themida", r"Packman", r"EXE Shield", r"WinRAR 32-bit SFX",
        r"WinZip 32-bit SFX", r"Install Stub 32-bit", r"P(E|e)tite",
        r"PKLITE32", r"y(o|0)da's (Protector|Crypter)", r"Ste@lth PE",
        r"PE\-Armor", r"KGB SFX", r"tElock", r"PEBundle", r"Crunch\/PE",
        r"Obsidium", r"nPack", r"PEX", r"PE Diminisher",
        r"North Star PE Shrinker", r"PC Guard for Win32", r"W32\.Jeefo",
        r"MEW [0-9]+", r"InstallAnywhere", r"Anskya Binder",
        r"BeRoEXEPacker", r"NeoLite", r"SVK\-Protector",
        r"Ding Boy's PE\-lock Phantasm", r"hying's PEArmor", r"E language",
        r"NSIS Installer", r"Video\-Lan\-Client", r"EncryptPE",
        r"HASP HL Protection", r"PESpin", r"CExe", r"UG2002 Cruncher",
        r"ACProtect", r"Thinstall", r"DBPE", r"XCR", r"PC Shrinker",
        r"AH(p|P)ack", r"ExeShield Protector",
        r"\* \[MSLRH\]", r"XJ \/ XPAL", r"Krypton", r"Stealth PE",
        r"Goats Mutilator", r"PE\-PACK", r"RCryptor", r"\* PseudoSigner",
        r"Shrinker", r"PC-Guard", r"PELOCKnt", r"WinZip \(32\-bit\)",
        r"EZIP", r"PeX", r"PE( |\-)Crypt", r"E(XE|xe)()?Stealth",
        r"ShellModify", r"Macromedia Windows Flash Projector\/Player",
        r"WARNING ->", r"PE Protector", r"Software Compress",
        r"PE( )?Ninja", r"Feokt", r"RLPack",
        r"Nullsoft( PIMP)? Install System", r"SDProtector Pro Edition",
        r"VProtector", r"WWPack32", r"CreateInstall Stub", r"ORiEN",
        r"dePACK", r"ENIGMA Protector", r"MicroJoiner", r"Virogen Crypt",
        r"SecureEXE", r"PCShrink", r"WinZip Self\-Extractor",
        r"PEiD\-Bundle", r"DxPack", r"Freshbind", r"kkrunchy"]

    def __init__(self, context="standalone"):
        """The context variable defines type of operation for the functions
        using visualisations. *standalone* saves figures as files, *notebook*
        displays them."""
        self.context = context
        self.labels = None
        self.simple_features = None
        self.simple_features_description = {}
        self.features = None
        self.clustering = {}

        self.target_labels = None
        self.target_features = None
        self.target_simple_features = None
        self.target_simple_features_description = {}


    # Helper: return a logarithmic bin of given value.
    def __log_bin(self, value, base=3):
        """Return a logarithmic bin of given value. * """
        if value is None:
            return None

        # Add base -1 to count so that 0 is in its own bin
        return int(log(value + base - 1, base))


    # Helper: make a string all lower cases
    def __normalise_string(self, string):
        """Get lower case string representation. * """
        if string is None:
            return None

        return string.lower()


    # Helper: simplify a string
    # Classified characters to nums, caps_c, caps_v, smal_c, smal_v
    def __simplify_string(self, string, distinguish_voyels=False):
        """Returns a simplified representation of the string where characters
        are mapped to their representatives. * """
        if string is None:
            return None

        nums = re.compile(r"[0-9]")
        caps = re.compile(r"[A-Z]")
        smal = re.compile(r"[a-z]")
        caps_c = re.compile(r"[QWRTPSDFGHJKLZXCVBNM]")
        caps_v = re.compile(r"[EYUIOA]")
        smal_c = re.compile(r"[qwrtpsdfghjklzxcvbnm]")
        smal_v = re.compile(r"[eyuioa]")

        string = nums.sub('0', string)

        if distinguish_voyels:
            string = caps_c.sub('B', string)
            string = caps_v.sub('A', string)
            string = smal_c.sub('b', string)
            string = smal_v.sub('a', string)
            return string

        string = caps.sub('A', string)
        string = smal.sub('a', string)
        return string


    # N-grams are simply all combinations of adjacent words or letters of length n that you can find in your source text.
    def __n_grams(self, string, n=3, reorder=False):
        """Returns a *set* of n-grams. If the iterable is smaller than n, it is
        returned itself. * """
        if string is None:
            return None

        if len(string) <= n:
            if reorder:
                return set(["".join(sorted(string))])
            return set([string])

        ngrams = set()
        for i in range(0, len(string) - n + 1):
            if reorder:
                ngrams.add("".join(sorted(string[i:i+n])))
            else:
                ngrams.add(string[i:i+n])

        return ngrams


    # Apply normalisation, simplification, n-gram to a string
    def __handle_string(self, string):
        """Apply normalisation, simplification and n-gram extraction to a
        string. If the string is missing (None) return empty list."""
        handled = self.__n_grams(
                   self.__simplify_string(
                       self.__normalise_string(string)
                   )
               )
        if handled is None:
            return []
        else:
            return handled


    # Mutator: extract all labels from dataframe
    def extract_labels(self, labels):
        """Extract labels into pandas data frame."""
        return pd.DataFrame(labels, index=["label"]).T


    # Accessor: get all labels
    def load_labels(self, labels):
        """Load labels into pandas data frame."""
        self.labels = self.extract_labels(labels)


    # Accessor: get all labels of target 
    def load_target_labels(self, target_labels):
        """Load labels of targets into pandas data frame."""
        self.target_labels = self.extract_labels(target_labels)


    # Mutator: extract all simple features from dataframe
    def extract_simple_features(self, simple_features):
        """Extract simple features form an external object into pandas data
        frame."""
        simple_features = pd.DataFrame(simple_features).T
        simple_features.fillna(False, inplace=True)
        # Convert to bool: True/False
        simple_features = simple_features.astype(bool)
        # Change to int: 1/0
        simple_features = simple_features.astype(int)

        # Aggregate features descriptions
        simple_features_description = {}
        for binary in simple_features:
            for token in simple_features[binary]:
                if token not in simple_features_description:
                    simple_features_description[token] = \
                        simple_features[binary][token]

        return simple_features, simple_features_description


    # Accessor: get all simple features
    def load_simple_features(self, simple_features):
        """Load simple features form an external object into pandas data
        frame."""
        self.simple_features , self.simple_features_description = \
            self.extract_simple_features(simple_features)


    # Mutator: extract all simple features of targets from dataframe
    def extract_target_simple_features(self, target_simple_features):
        """Extract simple features form the target object into pandas data
        frame."""

        if self.simple_features is None or self.simple_features_description is None:
            if self.simple_features is None:
                print "Load simple_features first"
            if self.simple_features_description is None:
                print "Load simple_features_description first"
            return

        target_simple_features = pd.DataFrame(target_simple_features).T
        target_simple_features.fillna(False, inplace=True)
        # Convert to bool: True/False
        target_simple_features = target_simple_features.astype(bool)
        # Change to int: 1/0
        target_simple_features = target_simple_features.astype(int)

        aggr_target_features = pd.DataFrame()

        # Aggregate simple features
        for col in self.simple_features:
            if col in target_simple_features:
                aggr_target_features[col] = target_simple_features[col]
            else:
                aggr_target_features[col] = 0

        # print "aggr_target_features: "
        # print aggr_target_features

        # Aggregate features descriptions
        target_simple_features_description = {}
        for binary in target_simple_features:
            for token in target_simple_features[binary]:
                # Drop the features that is not in the model
                if token not in self.simple_features_description:
                    print(token + " is not in clustering")
                    continue
                if token not in target_simple_features_description:
                    target_simple_features_description[token] = \
                        target_simple_features[binary][token]

        # Fillna to those features that are in the clustering but not in the target
        for binary in self.simple_features:
            for token in self.simple_features[binary]:
                if token not in target_simple_features_description:
                    print(token + " is not in target. Fill in none for this field.")
                    target_simple_features_description[token] = None
                    target_simple_features[binary][token] = None

        target_simple_features = aggr_target_features

        return target_simple_features, target_simple_features_description


    # Accessor: get all simple features of targets
    def load_target_simple_features(self, target_simple_features):
        """Load simple features of targets form an external object into pandas data
        frame."""
        self.target_simple_features , self.target_simple_features_description = \
            self.extract_target_simple_features(target_simple_features)


    # Export labels and simple features to a csv file
    def export_simple_dataset(self, filename="simple_dataset.csv"):
        """Export a dataset consisting of malware labels and *simple* features
        to CSV formatted file."""
        # Check if data and labels are loaded
        if self.simple_features is None:
            print "Please load simple features first."
            return

        if self.labels is None:
            print "Please load labels first."
            return

        simple_dataset = pd.concat([self.simple_features, self.labels], axis=1)
        simple_dataset.to_csv(filename)


    def simple_feature_category(self, category="properties"):
        """Get simple feature data frame containing only features form selected
        category."""
        if self.simple_features is None:
            print "Simple features are not loaded. Please load them before \
                   extracting categories."
            return None

        return self.simple_features.loc[:, self.SIMPLE_CATEGORIES[category]]


    # Mutator: extract all features from dataframe
    def extract_features(self, features, include_API_calls=False, \
                      include_API_calls_count=False):
        """Extract features form an external object into pandas data frame."""
        my_features = {}
        for i in features:
            my_features[i] = {}

            # Exponentially bin the binary size and timestamp
            my_features[i][":meta:size"] = self.__log_bin(features[i]["size"])
            my_features[i][":meta:timestamp"] = \
                self.__log_bin(features[i]["timestamp"])

            # Handle ExifTool output
            exif = ["FileDescription", "OriginalFilename", "magic_byte"]
            for e in exif:
                for j in self.__handle_string(features[i][e]):
                    my_features[i][":meta:" + j] = 1

            # Is the binary signed?
            my_features[i][":sign:signed"] = int(features[i]["signed"])
            # And other signature features
            signature = ["Comments", "ProductName", "LegalCopyright",
                         "InternalName", "CompanyName"]
            for s in signature:
                for j in self.__handle_string(features[i][s]):
                    my_features[i][":sign:" + j] = 1

            # Extract packer
            regexps = [re.compile(pattern) for pattern in self.PATTERNS]
            if features[i]["packer"] is not None:
                for packer in features[i]["packer"]:
                    for regexp in regexps:
                        if regexp.search(packer):
                            my_features[i][":pack:" + regexp.pattern] = 1
                            break

            # Vectorise PEFs
            for j in features[i]["languages"]:
                j_norm = self.__normalise_string(j)
                my_features[i][":pef:lang:" + j_norm] = 1
            # Get number of languages
            my_features[i][":count:lang"] = len(features[i]["languages"])
            # TODO: handle *section_attrs* and *resource_attrs*

            # Categorise static imports
            # TODO: use binning for this count
            my_features[i][":simp:count"] = \
                features[i]["static_imports"]["count"]
            static_imports_dlls = features[i]["static_imports"].keys()
            static_imports_dlls.remove("count")
            # Count static imports
            my_features[i][":count:simp"] = len(static_imports_dlls)
            for j in static_imports_dlls:
                my_features[i][":simp:" + j] = 1
                # TODO: include API calls?
                if include_API_calls:
                    for k in features[i]["static_imports"][j]:
                        my_features[i][":simp:" + j + ":" + k] = 1
                # Count static imports API calls
                if include_API_calls_count:
                    my_features[i][":count:simp:" + j] = \
                        len(features[i]["static_imports"][j])

            # Categorise dynamic imports
            if features[i]["mutex"] is not None:
                for mutex in features[i]["mutex"]:
                    for j in self.__handle_string(mutex):
                        my_features[i][":dimp:mutex:" + j] = 1
                # Count mutexes
                my_features[i][":count:mutex"] = len(features[i]["mutex"])
            for process in features[i]["processes"]:
                my_features[i][":dimp:proc:" + process] = 1
            # Count processes
            my_features[i][":count:proc"] = len(features[i]["processes"])
            for di in features[i]["dynamic_imports"]:
                my_features[i][":dimp:" + di] = 1
            # Count dynamic imports
            my_features[i][":count:dimp"] = \
                len(features[i]["dynamic_imports"])

            # File operations
            # TODO: tell apart different file operations by prefixing
            # Files touched
            touch = ["file_read", "file_written", "file_deleted", "file_copied",
                     "file_renamed"]
            for t in touch:
                for f in features[i][t]:
                    my_features[i][":file:touch:" + f] = 1
            # TODO: better binning (linear not logarithmic)
            # File numbers
            operation_number = [("all", "files_operations"),
                                ("read", "files_read"),
                                ("written", "files_written"),
                                ("deleted", "files_deleted"),
                                ("copied", "files_copied"),
                                ("renamed", "files_renamed"),
                                ("opened", "files_opened"),
                                ("exists", "files_exists"),
                                ("failed", "files_failed")]
            for o in operation_number:
                my_features[i][":file:count:" + o[0]] = \
                    self.__log_bin(features[i][o[1]])
                my_features[i][":count:file:" + o[0]] = \
                    features[i][o[1]]

            # Networking
            # TODO: include subnets
            # TODO: tell apart type of connection: prefix features with "tcp",
            #       "udp", "dns"
            for tcp in features[i]["tcp"]:
                my_features[i][":net:" + tcp] = 1
            for udp in features[i]["udp"]:
                my_features[i][":net:" + udp] = 1
            for dns in features[i]["dns"]:
                my_features[i][":net:" + dns] = 1
                for j in features[i]["dns"][dns]:
                    my_features[i][":net:" + j] = 1
            for http in features[i]["http"]:
                my_features[i][":net:" + features[i]["http"][http]["host"]] \
                    = 1
            # Count tcp addresses
            my_features[i][":count:tcp"] = len(features[i]["tcp"])
            # Count udp addresses
            my_features[i][":count:udp"] = len(features[i]["udp"])
            # Count dns addresses
            my_features[i][":count:dns"] = len(features[i]["dns"])
            # Count http addresses
            my_features[i][":count:http"] = len(features[i]["http"])

            # Register operations
            for rw in features[i]["regkey_written"]:
                my_features[i][":reg:write:" + rw] = 1
            # Count register keys written
            my_features[i][":count:reg:write"] = \
                len(features[i]["regkey_written"])
            for rd in features[i]["regkey_deleted"]:
                my_features[i][":reg:del:" + rd] = 1
            # Count register keys written
            my_features[i][":count:reg:del"] = \
                len(features[i]["regkey_deleted"])

            # Windows API
            # TODO: better binning (linear not logarithmic)
            for wapi in features[i]["api_stats"]:
                my_features[i][":win:" + wapi] = \
                    self.__log_bin(features[i]["api_stats"][wapi])
            # Count Windows API calls
            my_features[i][":count:wapi"] = len(features[i]["api_stats"])

        # Make Pandas DataFrame from the dictionary
        features_pd = pd.DataFrame(my_features).T
        # TODO: the operation below cannot tell apart missing vales and None
        features_pd.fillna(0, inplace=True)
        return features_pd


    # Accessor: get all features  
    def load_features(self, features, include_API_calls=False, \
                      include_API_calls_count=False):
        """Load features form an external object into pandas data frame."""
        self.features = self.extract_features(features, include_API_calls,
                                              include_API_calls_count)


    # Export labels and all features to a csv file
    def export_dataset(self, filename="dataset.csv"):
        """Export a dataset consisting of malware labels and features to CSV
        formatted file."""
        # Check if data and labels are loaded
        if self.features is None:
            print "Please load features first."
            return

        if self.labels is None:
            print "Please load labels first."
            return

        dataset = pd.concat([self.features, self.labels], axis=1)
        dataset.to_csv(filename, encoding='utf-8')


    def feature_category(self, category="static", complement=False):
        """Get feature data frame containing only features form selected
        category (or their complement)."""
        def pull_names(obj, prefix=""):
            ret = []
            if isinstance(obj, dict):
                if prefix: ret.append(prefix)
                for key in obj:
                    ret += pull_names(obj[key], prefix + key)
                return ret
            elif isinstance(obj, list):
                for i in obj:
                    ret.append(prefix + i)
                return ret

        if self.features is None:
            print "Features are not loaded. Please load them before extracting \
                   categories."
            return None

        # Pull all possible categories
        categories = self.CATEGORIES.keys()
        for cat in self.CATEGORIES:
            categories += pull_names(self.CATEGORIES[cat])

        # Check if chosen category is available
        if category not in categories:
            print "Chosen category:", category, "is not available.\n\
                   please choose one of the following:"
            print ", ".join(categories)
            return None

        # Get a list of specified prefixes
        if category == "static" or category == "dynamic" or category == "count":
            category = pull_names(category)
        else:
            category = [category]

        extract = []
        # TODO: what if we want exact match but most starts with word
        for col in self.features:
            for c in category:
                if complement and not col.startswith(c):
                    extract.append(col)
                elif not complement and col.startswith(c):
                    extract.append(col)

        return self.features.loc[:, extract]


    # Prune useless features; remove sparse features
    def filter_dataset(self, dataset=None, feature_coverage=0.1,
                       complement=False):
        """Prune features that are useless."""
        if dataset is None:
            dataset = self.features.copy()

        # Remove sparse features
        row_count = dataset.shape[0]
        remove_features = []
        for col in dataset:
            zero_count = 0.0
            for row in dataset[col]:
                if not row:
                    zero_count += 1
            # XOR
            if complement != (row_count-zero_count)/row_count<feature_coverage:
                remove_features.append(col)
        dataset.drop(remove_features, axis=1, inplace=True)

        return dataset


    # Detect outliers and suspect_outliers
    def detect_abnormal_behaviour(self, count_dataset=None, figures=True):
        """Detect samples that behave significantly different than others."""
        if count_dataset is None:
            # Pull all count features
            count_features = self.feature_category(":count:")
            meta_size = self.feature_category(":meta:size")
            simp_count = self.feature_category(":simp:count")
            count_dataset = pd.concat([count_features, meta_size, \
                                      simp_count], axis=1)

        if not figures:
            ret = {}
        for f in count_dataset:
            # Produce boxplots
            if figures:
                sns.boxplot(count_dataset[f])
                sns.swarmplot(count_dataset[f], color=".25")
                plt.title("Abnormal behaviour detection for " + f)
                if self.context == "notebook":
                    plt.show()
                else:
                    plt.savefig("abnormal_behaviour_" + f.replace(":", "_") + \
                                ".png")
                    plt.close()

            # Get list of local outliers
            f_1_quartile = count_dataset[f].quantile(0.25)
            f_3_quartile = count_dataset[f].quantile(0.75)
            f_IQR = f_3_quartile - f_1_quartile
            f_outliers = []
            f_suspected_outliers = []
            for s in count_dataset[f].index:
                if count_dataset[f][s] > f_3_quartile+3*f_IQR or \
                        count_dataset[f][s] < f_1_quartile-3*f_IQR:
                    f_outliers.append(s)
                    continue
                if count_dataset[f][s] > f_3_quartile+1.5*f_IQR or \
                        count_dataset[f][s] < f_1_quartile-1.5*f_IQR:
                    f_suspected_outliers.append(s)
            if figures:
                print f
                print "Outliers: ", ", ".join(f_outliers)
                print "Suspected outliers: ", ", ".join(f_suspected_outliers)
                print "------------------------------------------------------------"
            else:
                ret[f] = {"outliers": f_outliers,
                          "suspect_outliers": f_suspected_outliers}
        if not figures:
            return pd.DataFrame(ret).T


    # Visualize labels using TSNE
    def visualise_data(self, data=None, labels=None, learning_rate=200,
                       fig_name="custom"):
        """Create t-Distributed Stochastic Neighbor Embedding for features and
        labels to help inspect the data."""
        if data is None:
            data = self.features
        if labels is None:
            labels = self.labels

        tsne = TSNE(learning_rate=learning_rate)
        tsne_fit = tsne.fit_transform(data)
        tsne_df = pd.DataFrame(tsne_fit, index=data.index, columns=['0', '1'])
        tsne_dfl = pd.concat([tsne_df, labels], axis=1)

        sns.lmplot("0", "1", data=tsne_dfl, fit_reg=False, hue="label",
                   scatter_kws={"marker":"D", "s":50}, legend_out=True)
        plt.title(fig_name + " (lr:" + str(learning_rate) + ")")
        if self.context == "notebook":
            plt.show()
        else:
            plt.savefig(fig_name + "_" + str(learning_rate) + ".png",
                        bbox_inches='tight', pad_inches=1.)
            plt.close()


    # Export selected features and labels to a csv file
    def save_dataset(self, filename="custom_dataset.csv", features=None, \
                     labels=None):
        """Export a dataset to CSV formatted file."""
        # Check if data and labels are loaded
        if features is None:
            print "You must indicate data to be saved."
            return

        if labels is None:
            print "You didn't indicate labels to be used. Internal labels will \
                   be used."
            if self.labels is None:
                print "Internal labels not available."
                return
            else:
                labels = self.labels

        dataset = pd.concat([features, labels], axis=1)
        dataset.to_csv(filename, encoding='utf-8')


    # Clustering features using DBSCAN
    def cluster_dbscan(self, features=None, eps=20.0, min_samples=5, dry=False):
        """Do *dbscan* clustering and return """
        if features is None:
            print "You didn't indicate features to be used. Internal features \
                will be used."
            if self.features is None:
                print "Internal features not available."
                return
            else:
                features = self.features

        dbscan = DBSCAN(eps=eps, min_samples=min_samples).fit(features)

        # TODO: Allow storing multiple clustering results based on parameters
        if dry:
            return {
                "eps":eps,
                "min_samples":min_samples,
                "clustering":pd.DataFrame(dbscan.labels_, index=features.index,
                                        columns=["label"])
            }
        else:
            # if "dbscan" not in self.clustering:
                # self.clustering["dbscan"] = {}
            # clustering_hash = "eps:%f&min_samples:%d" % (eps, min_samples)
            self.clustering["dbscan"] = {
            # self.clustering["dbscan"][clustering_hash] = {
                "eps":eps,
                "min_samples":min_samples,
                "clustering":pd.DataFrame(dbscan.labels_, index=features.index,
                                        columns=["label"])
            }


    # Clustering features using HDBSCAN
    def cluster_hdbscan(self, features=None, min_samples=1, \
                        min_cluster_size=6, dry=False):
        """Do *hdbscan* clustering and return """
        if features is None:
            print "You didn't indicate features to be used. Internal features \
                will be used."
            if self.features is None:
                print "Internal features not available."
                return
            else:
                features = self.features

        # hdbscan = hdbscan.HDBSCAN(min_samples=min_samples, \
        #                   min_cluster_size=min_cluster_size, prediction_data=True)
        hdbscan_fit = hdbscan.HDBSCAN(min_samples=min_samples, \
                          min_cluster_size=min_cluster_size, prediction_data=True).fit(features)
        hdbscan_stats = np.column_stack([hdbscan_fit.labels_,
                                         hdbscan_fit.probabilities_,
                                         hdbscan_fit.outlier_scores_])

        # TODO: Allow storing multiple clustering results based on parameters
        if dry:
            return {
                "min_samples":min_samples,
                "min_cluster_size":min_cluster_size,
                "clustering":pd.DataFrame(hdbscan_stats, index=features.index,
                                          columns=["label", "probability",
                                                   "outlier_score"])
            }
        else:
            # if "hdbscan" not in self.clustering:
                # self.clustering["hdbscan"] = {}
            # clustering_hash = "min_samples:%s&min_cluster_size:%d" % \
                # (min_samples, min_cluster_size)
            # self.clustering["hdbscan"][clustering_hash] = {
            self.clustering["hdbscan"] = {
                "min_samples":min_samples,
                "min_cluster_size":min_cluster_size,
                "clustering":pd.DataFrame(hdbscan_stats, index=features.index,
                                          columns=["label", "probability",
                                                   "outlier_score"])
            }
            print("self.clustering[hdbscan]")
            print(self.clustering["hdbscan"])


    # Export clustering results to a file
    def save_clustering_results(self, loader, save_location=""):
        """Update JSONs report files with clustering results"""
        # TODO: Allow storing multiple clustering results based on parameters
        hdbscan_root = ["info", "clustering", "hdbscan"]
        hdbscan_paths = {"label":"clusterID",
                         "probability":"clusterProbability",
                         "outlier_score":"outlierScore"}
        if "hdbscan" in self.clustering:
            loader.update_binaries(self.clustering["hdbscan"]["clustering"],
                                   hdbscan_root, hdbscan_paths)
            loader.update_binaries(self.clustering["hdbscan"]["min_samples"],
                                   hdbscan_root, "min_samples")
            loader.update_binaries(
                self.clustering["hdbscan"]["min_cluster_size"], hdbscan_root,
                "min_cluster_size")

        dbscan_root = ["info", "clustering", "dbscan"]
        dbscan_paths = {"label":"clusterID"}
        if "dbscan" in self.clustering:
            loader.update_binaries(self.clustering["dbscan"]["clustering"],
                                   dbscan_root, dbscan_paths)
            loader.update_binaries(self.clustering["dbscan"]["eps"],
                                   dbscan_root, "eps")
            loader.update_binaries(self.clustering["dbscan"]["min_samples"],
                                   dbscan_root, "min_samples")

        loader.save_binaries(save_location)


    def anomaly_detection(self, samples=None, labels=None,
                          probability_threshold=0.9, outlier_threshold=0.5,
                          homogeneity_threshold=0.2):
        """Detect anomalies in clustering using samples classified as noise and
        low probability cluster membership."""
        if "hdbscan" not in self.clustering:
            print "Soft clustering is needed for *anomaly detection*.", \
                "Currently only *HDBSCAN* is supported."
            return

        if labels is None:
            labels = self.labels
        if samples is None:
            samples = self.clustering["hdbscan"]["clustering"]
        sample = samples.copy()
        sample.rename(columns={"label": "cluster"}, inplace=True)
        sample = pd.concat([sample, labels], axis=1)

        anomalies = {}

        anomalies["outliers"] = sample[sample.cluster == -1].index.tolist()
        # TODO: cluster ID and samples most similar to given outlier

        # Clustered as X but below threshold
        anomalies["low_probability"] = \
            sample.loc[sample.probability < probability_threshold] \
                  .loc[sample.cluster != -1].index.tolist()

        # High outlier score
        anomalies["high_outlier_score"] = \
            sample.loc[sample.outlier_score > outlier_threshold] \
                  .loc[sample.cluster != -1].index.tolist()

        # Within cluster inconsistencies - detect non-homogeneous clusters
        anomalies["homogeneity_suspects"] = {}
        for i in set(sample["cluster"].tolist()):
            c = collections.Counter(
                sample[sample.cluster == i]["label"].tolist())
            total = float(sum(c.values()))
            suspicious = [j for j in c if c[j]/total < homogeneity_threshold]

            anomalies["homogeneity_suspects"][i] = []
            for j in suspicious:
                anomalies["homogeneity_suspects"][i] += \
                    sample.loc[sample.cluster == i].loc[sample.label == j] \
                    .index.tolist()

        return anomalies


    def compare_sample(self, sample, amend=False):
        """Compare new sample with current clustering."""
        if isinstance(sample, Instance):
            # Retrieve cluster ID
            # TODO: this alters cluster structure-retraining needs to be removed
            features = self.extract_features({"?"+sample.name: sample.features})
            # simple_features = self.extract_simple_features(
                # {"?"+sample.name: sample.basic_features})
            # label = self.extract_labels({"?"+sample.name: sample.label})
            extended_features = pd.concat([self.features, features])
            extended_features.fillna(0, inplace=True)
            clustering = self.cluster_hdbscan(features=extended_features, \
                                              dry=True)
            clustering_result = clustering["clustering"].loc["?"+sample.name]

            # TODO: return samples that are the most similar to the analysed one

            # Save clustering information to the sample's JSON
            if amend:
                root = ["info", "clustering", "hdbscan"]
                sample.update(clustering_result["label"], root+["clusterID"])
                sample.update(clustering_result["probability"],
                            root+["clusterProbability"])
                sample.update(clustering_result["outlier_score"],
                            root+["outlierScore"])
                sample.update(clustering["min_samples"], "min_samples")
                sample.update(clustering["min_cluster_size"], \
                              "min_cluster_size")
                sample.save_json(os.path.dirname(sample.json_path)+"/")
        # TODO: handle more than one test sample
        elif isinstance(sample, Loader):
            clustering_result = pd.DataFrame()

        return clustering_result


    def assess_clustering(self, clustering, labels, data=None,
                          discard_noise=False):
        """Assess clusters fit according to variety of metrics."""
        def performance_metric(clustering, labels, data, noise):
            performance_metrics = {}
            performance_metrics["Adjusted Random Index"] = \
                metrics.adjusted_rand_score(labels, clustering)
            performance_metrics["Adjusted Mutual Information Score"] = \
                metrics.adjusted_mutual_info_score(labels, clustering)
            performance_metrics["Homogeneity"] = \
                metrics.homogeneity_score(labels, clustering)
            performance_metrics["Completeness"] = \
                metrics.completeness_score(labels, clustering)
            performance_metrics["V-measure"] = \
                metrics.v_measure_score(labels, clustering)

            if data is None or noise:
                return performance_metrics
            performance_metrics["Silhouette Coefficient"] = \
                metrics.silhouette_score(data, np.array(clustering))

            return performance_metrics

        cluster_label = clustering["label"].tolist()
        ground_label = labels["label"].tolist()

        if discard_noise:
            clustering= []
            labels = []
            noise_clustering = []
            noise_labels = []
            for c, g in itertools.izip(cluster_label, ground_label):
                if c == -1:
                    noise_clustering.append(c)
                    noise_labels.append(g)
                else:
                    clustering.append(c)
                    labels.append(g)
            # print performance_metric(noise_clustering, noise_labels, \
            #                          data, True)
        else:
            clustering = cluster_label
            labels = ground_label

        return performance_metric(clustering, labels, data, discard_noise)


    def clustering_label_distribution(self, clustering, labels, plot=False):
        """Get statistics about number of ground truth labels per cluster."""
        cluster_ids = set(clustering["label"].tolist())
        labels_ids = set(labels["label"].tolist())
        cluster_distribution = {}
        for i in cluster_ids:
            cluster_distribution[i] = {}
            for j in labels_ids:
                cluster_distribution[i][j] = 0

        for i in clustering.index:
            cluster_distribution[clustering["label"][i]][labels["label"][i]] \
                += 1

        if plot:
            for i in cluster_distribution:
                fig = plt.figure()
                ax = fig.add_subplot(111)
                yticks = []
                counter = 0
                for j in cluster_distribution[i]:
                    if cluster_distribution[i][j]:
                        ax.barh(counter, cluster_distribution[i][j])
                        counter += 1
                        yticks.append(j)
                yticks_range = [l+.4 for l in range(len(yticks))]
                plt.yticks(yticks_range, yticks)
                ax.set_ylim([0, yticks_range[-1]+.4])
                plt.title("Cluster: %d" % i)
                if self.context == "notebook":
                    plt.show()
                else:
                    plt.savefig("cluster_%d%s" % (i, ".png"), bbox_inches="tight")
                    plt.close()
        else:
            cluster_distribution = pd.DataFrame(cluster_distribution).T
            cluster_distribution.index.name = "cluster_id"
            return cluster_distribution


    # ******************** Cindy's code ********************


    # Do machine learning using HDBSCAN
    # Return hdbscan_fit
    def hdbscan_fit(self, features=None, min_samples=1, min_cluster_size=6):
        hdbscan_fit = hdbscan.HDBSCAN(min_samples=min_samples, \
                          min_cluster_size=min_cluster_size, prediction_data=True).fit(features)
        return hdbscan_fit


    # Learn and predict featuresPredict using featuresLabel
    def cluster_hdbscan_classifer(self, featuresLabel=None, featuresPredict=None, min_samples=1, \
                        min_cluster_size=6, dry=False):
        """Do *hdbscan* clustering and return """
        if featuresLabel is None:
            print "You didn't indicate features to be used. Internal features \
                will be used."
            if self.featuresLabel is None:
                print "Internal features not available."
                return
            else:
                featuresLabel = self.features

        if featuresPredict is None:
            print "No features are specified to predict"
            return

        hdbscan_fit = hdbscan.HDBSCAN(min_samples=min_samples, \
                          min_cluster_size=min_cluster_size, prediction_data=True).fit(featuresLabel)
        hdbscan_predictions = hdbscan.approximate_predict(hdbscan_fit, featuresPredict)
        print(hdbscan_predictions)


    # Predict by the cluster passed by
    # Return the prediction results
    def classifier_hdbscan(self, cluster, featuresPredict=None, do_print=False):
        hdbscan_predictions = hdbscan.approximate_predict(cluster, featuresPredict)
        if do_print is True:
            print(hdbscan_predictions)
        return hdbscan_predictions


    # Count the number of features each target has
    # Return a dict (idx, counter of features)
    def count_features(self, classfier=None, target_features=None, label=-1, do_print=False, \
                        use_counter=True):
        if classfier is None:
            print("No classifer specified")
            return

        if target_features is None:
            if self.target_features is None:
                print("No target features specified")
                return
            else:
                target_features = self.target_features

        pred_label = classfier[0]
        label_arr = np.where(pred_label==label)
        label_arr = label_arr[0]
        target_features_arr = target_features.as_matrix()
        target_counter_dict = {}

        for item in label_arr:
            cur_features = np.array(target_features_arr[item])
            if use_counter is True:
                target_counter_dict[item] = collections.Counter(cur_features)
            else:
                target_counter_dict[item] = cur_features

        if do_print is True:
            print(target_counter_dict)

        return target_counter_dict


    # Print features that exist
    def count_and_print_features(self, classfier=None, target_features=None, label=-1, do_print=False):
        if classfier is None:
            print("No classifer specified")
            return

        if target_features is None:
            if self.target_features is None:
                print("No target features specified")
                return
            else:
                target_features = self.target_features

        pred_label = classfier[0]
        label_arr = np.where(pred_label==label)
        label_arr = label_arr[0]
        target_features_arr = target_features.as_matrix()
        target_counter_dict = {}
        col_index_array = target_features.columns.values

        for item in label_arr:
            cur_features = np.array(target_features_arr[item])
            cur_features_label = []
            for idx in range(0, len(cur_features)):
                if cur_features[idx] == 1:
                    cur_features_label.append(col_index_array[idx])
        #             print col_index_array[idx]
        #     print cur_features_label
            target_counter_dict[item] = cur_features_label

        if do_print is True:
            print(target_counter_dict)
            
        return target_counter_dict


    # Calculate avr similarities for each key values
    def calc_similarity_self(self, count_features_arr, do_print=False):
        res = {}
        for item in count_features_arr:
            cur_vector = count_features_arr[item]
            similarities = []
            for other_item in count_features_arr:
                if item == other_item:
                    continue
                else:
                    vec1 = np.array([count_features_arr[item]])
                    vec2 = np.array([count_features_arr[other_item]])
                    cur_sim = cosine_similarity(vec1, vec2)
                    similarities.append(cur_sim)
        #             print cur_sim
            avr_sim = reduce(lambda x, y: x + y, similarities) / len(similarities)
            res[item] = avr_sim

        if do_print:
            print(res)
        
        return res        


    # Calculate avr similarities for each key and a sample array
    def calc_similarity_sample(self, count_features_arr, sample_features_arr, do_print=False):
        res = {}
        for item in count_features_arr:
            cur_vector = count_features_arr[item]
            similarities = []
            for other_item in sample_features_arr:
                vec1 = np.array([count_features_arr[item]])
                vec2 = np.array([sample_features_arr[other_item]])
                cur_sim = cosine_similarity(vec1, vec2)
                similarities.append(cur_sim)
            avr_sim = reduce(lambda x, y: x + y, similarities) / len(similarities)
            res[item] = avr_sim

        if do_print:
            print(res)
        
        return res            


    # Union features and sorted by the number of times each features appear
    def union_features(self, in_dict, is_sorted=True):
        map_a = {}
        for key in in_dict:
            cur_features = in_dict[key]
        #     print cur_features
            for fea in cur_features:
                if fea in map_a:
                    map_a[fea] += 1
                else:
                    map_a[fea] = 1
        
        if not is_sorted:
            return map_a        
        else:
            sorted_map = sorted(map_a.items(), key=operator.itemgetter(1), reverse=True)
            return sorted_map


    # ******************** Supervised ML Model ********************

    # A driver function for Machine learning classifiers
    def MLClassifier(self, algorithm='random_forest', features=None, labels=None, target_features=None, \
                        target_labels=None, doPredict=True, default=True):
        if features is None:
            print "You didn't indicate features to be used. Internal features \
                will be used."
            if self.features is None:
                print "Internal features not available."
                return
            else:
                features = self.simple_features

        if labels is None:
            print "You didn't indicate labels to be used. Internal labels \
                will be used."
            if self.labels is None:
                print "Internal labels not available."
                return
            else:
                labels = self.labels

        if not doPredict:
            if target_features is None:
                print "You didn't indicate target features to be used. Internal target features \
                    will be used."
                if self.target_features is None:
                    print "Internal target features not available."
                    return
                else:
                    target_features = self.target_simple_features

            if target_labels is None:
                print "You didn't indicate target labels to be used. Internal target labels \
                    will be used."
                if self.target_labels is None:
                    print "Internal target labels not available."
                    return
                else:
                    target_labels = self.target_labels

        if algorithm is 'random_forest':
            if default:
                return self.RandomForestClassifier(features, labels, target_features, target_labels, doPredict)
            else:
                n_estimators = input("Please input n_estimators: ")
                return self.RandomForestClassifier(features, labels, target_features, target_labels, doPredict, n_estimators)

        if algorithm is 'logistic_regression':
            return self.LogisticRegressionClassfier(features, labels, target_features, target_labels, doPredict)

        if algorithm is 'knn':
            if default:
                return self.KNNClassifier(features, labels, target_features, target_labels, doPredict)
            else:
                n_neighbors = input("Please input n_neighbors: ")
                return self.KNNClassifier(features, labels, target_features, target_labels, doPredict, n_neighbors)

        if algorithm is 'decision_tree':
            if default:
                return self.DecisionTreeClassifier(features, labels, target_features, target_labels, doPredict)
            else:
                criterion = input("Please input criterion: ")
                random_state = input("Please input random_state: ")
                max_depth = input("Please input max_depth: ")
                min_samples_leaf = input("Please input min_samples_leaf: ")
                return self.DecisionTreeClassifier(features, labels, target_features, target_labels, doPredict, \
                                                    criterion, random_state, max_depth, min_samples_leaf)            


    # Classifer using random forest model
    def RandomForestClassifier(self, features=None, labels=None, target_features=None, \
                                target_labels=None, doPredict=True, n_estimators=5):

        clf = RandomForestClassifier(n_estimators=n_estimators)
        model = clf.fit(features, labels)

        if not doPredict:
            return model

        pred = clf.predict(target_features)
        score = metrics.accuracy_score(pred, target_labels)
        return score


    # Classifer using logistic regression model
    def LogisticRegressionClassfier(self, features=None, labels=None, target_features=None, \
                                target_labels=None, doPredict=True):
        log_reg = LogisticRegression()
        model = log_reg.fit(features, labels)

        if not doPredict:
            return model

        pred = log_reg.predict(target_features)
        score = metrics.accuracy_score(pred, target_labels)
        return score


    # Classifer using KNN model
    def KNNClassifier(self, features=None, labels=None, target_features=None, \
                                target_labels=None, doPredict=True, n_neighbors=10):
        knn = KNeighborsClassifier(n_neighbors=n_neighbors)
        model = knn.fit(features, labels)

        if not doPredict:
            return model
        
        pred = knn.predict(target_features)
        score = metrics.accuracy_score(pred, target_labels)
        return score


    # Classifer using decision tree model
    def DecisionTreeClassifier(self, features=None, labels=None, target_features=None, \
                                target_labels=None, doPredict=True, criterion="entropy", \
                                random_state=100, max_depth=3, min_samples_leaf=5):
        clf = DecisionTreeClassifier(criterion=criterion, random_state=random_state, \
                                    max_depth=max_depth, min_samples_leaf=min_samples_leaf)
        model = clf.fit(features, labels)

        if not doPredict:
            return model

        pred = clf.predict(target_features)
        score = metrics.accuracy_score(pred, target_labels)
        return score


    # Cross Validation using different models
    def CrossValidation(self, algorithm='random_forest', features=None, labels=None, \
                                    cv=10, scoring='accuracy', default=True):
        if features is None:
            print "You didn't indicate features to be used. Internal features \
                will be used."
            if self.features is None:
                print "Internal features not available."
                return
            else:
                features = self.simple_features

        if labels is None:
            print "You didn't indicate labels to be used. Internal labels \
                will be used."
            if self.labels is None:
                print "Internal labels not available."
                return
            else:
                labels = self.labels

        if algorithm is 'random_forest':
            if default:
                model = RandomForestClassifier()
            else:
                n_estimators = input("Please input n_estimators: ")
                model = RandomForestClassifier(n_estimators=n_estimators)

        if algorithm is 'logistic_regression':
            model = LogisticRegression()

        if algorithm is 'knn':
            if default:
                model = KNeighborsClassifier()
            else:
                n_neighbors = input("Please input n_neighbors: ")
                model = KNeighborsClassifier(n_neighbors=n_neighbors)

        if algorithm is 'decision_tree':
            if default:
                model = DecisionTreeClassifier()
            else:
                criterion = input("Please input criterion: ")
                random_state = input("Please input random_state: ")
                max_depth = input("Please input max_depth: ")
                min_samples_leaf = input("Please input min_samples_leaf: ")
                model = DecisionTreeClassifier(criterion=criterion, random_state=random_state, \
                                                max_depth=max_depth, min_samples_leaf=min_samples_leaf)   

        cross_score = cross_val_score(model, features, labels, cv=cv, scoring=scoring)

        return cross_score.mean()


    # List the importance of each feature to the machine learning model
    def RandomForestFeatureImportance(self, features=None, labels=None, n_estimators=5, \
                                        doPrint=False):
        col_index_array = features.columns.values
        clf = RandomForestClassifier(n_estimators=n_estimators)
        clf.fit(features, labels)
        feature_dict = {}

        importances = clf.feature_importances_
        std = np.std([tree.feature_importances_ for tree in clf.estimators_], 
                        axis=0)
        indices = np.argsort(importances)[::-1]

        if doPrint:
            print("Feature ranking:")
            for f in range(features.shape[1]):
                cur_ind = indices[f]
                print("%d. feature: %s (%f)" % (f + 1, col_index_array[cur_ind], importances[cur_ind]))

        for f in range(features.shape[1]):
            cur_ind = indices[f]
            key = col_index_array[cur_ind]
            feature_dict[key] = importances[cur_ind]

        sorted_dict = sorted(feature_dict.items(), key=operator.itemgetter(1), reverse=True)

        return sorted_dict    



class Loader(object):
    """Loads instances for analysis and give possibility to extract properties
    of interest."""
    def __init__(self):
        self.binaries = {}
        self.binaries_location = ""
        self.binaries_updated = False


    def load_binaries(self, directory):
        """Load all binaries' reports from given directory."""
        self.binaries_location = directory + "/"
        for f in os.listdir(directory):
            self.binaries[f] = Instance()
            self.binaries[f].load_json(directory+"/"+f, f)
            self.binaries[f].label_sample()
            self.binaries[f].extract_features()
            self.binaries[f].extract_basic_features()


    # Load report binaries
    # Use load_report_json instead of load_json
    def load_binaries_report(self, directory, label=None):
        self.binaries_location = directory + "/"
        for f in os.listdir(directory):
            self.binaries[f] = Instance()
            self.binaries[f].load_report_json(directory+"/"+f, f)
            if label is None:
                self.binaries[f].label_sample()
            else:
                self.binaries[f].label_sample(external_labels=label)
            self.binaries[f].extract_features()
            self.binaries[f].extract_basic_features()


    def update_binaries(self, elements, root, locations):
        """Append `elements` to the loaded JSONs at given `locations`."""
        if isinstance(elements, pd.DataFrame) and isinstance(locations, dict):
            self.binaries_updated = True
            for i in elements.index:
                for j in elements.columns:
                    self.binaries[i].update(elements[j][i], root+[locations[j]])
        elif isinstance(locations, str):
            self.binaries_updated = True
            for i in self.binaries:
                self.binaries[i].update(elements, root+[locations])


    # Save updated binaries to a JSON file
    def save_binaries(self, alternative_location=""):
        """Save the binaries to given location if they have been updated."""
        if self.binaries_updated:
            save_location = self.binaries_location
            if alternative_location:
                save_location = alternative_location
                if save_location[-1] != "/":
                    save_location += "/"

            # Create directory if it does not exist
            if not os.path.exists(save_location):
                os.makedirs(save_location)

            for f in self.binaries:
                self.binaries[f].save_json(save_location)
            self.binaries_updated = False
        else:
            print "The binaries haven't been updated. No need to save them."


    def get_labels(self):
        """Return binary labels as a labelled dictionary."""
        labels = {}
        for i in self.binaries:
            labels[i] = self.binaries[i].label
        return labels


    def get_labels_md5(self):
        """Return binary labels as a labelled dictionary."""
        labels = {}
        for i in self.binaries:
            if "_" not in i:
                idx = i.index('.')
            else:
                idx = i.index('_')
            key = i[:idx]
            labels[key] = self.binaries[i].label
        return labels


    def get_features(self):
        """Return complex binary features as a labelled dictionary."""
        features = {}
        for i in self.binaries:
            features[i] = self.binaries[i].features
        return features


    def get_simple_features(self):
        """Return simplified binary features as a labelled dictionary."""
        simple_features = {}
        for i in self.binaries:
            simple_features[i] = self.binaries[i].basic_features
        return simple_features


    # Edit the key name to md5
    def get_simple_features_md5(self):
        simple_features = {}
        for i in self.binaries:
            if "_" not in i:
                idx = i.index('.')
            else:
                idx = i.index('_')
            key = i[:idx]
            simple_features[key] = self.binaries[i].basic_features
        return simple_features       


class Instance(object):
    """Machine Learning for Cuckoo."""
    LABEL_SIGNIFICANCE_COUNT = 5
    POSITIVE_RATE = 2 * LABEL_SIGNIFICANCE_COUNT

    def __init__(self):
        self.json_path = ""
        self.name = ""
        self.report = None
        self.total = None
        self.positives = None
        self.scans = None
        self.label = None
        self.features = {}
        self.basic_features = {}


    def load_json(self, json_file, name="unknown"):
        """Load JSON formatted malware report. It can handle both a path to
        JSON file and a dictionary object."""
        if isinstance(json_file, str):
            self.json_path = json_file
            with open(json_file, "r") as malware_report:
                try:
                    self.report = json.load(malware_report)
                except ValueError, error:
                    print >> sys.stderr, "Could not load file;", \
                        malware_report, "is not a valid JSON file."
                    print >> sys.stderr, "Exception: %s" % str(error)
                    sys.exit(1)
        elif isinstance(json_file, dict):
            self.report = json_file
        else:
            # Unknown binary format
            print >> sys.stderr, "Could not load the data *", json, "* is of " \
                "unknown type: ", type(json), "."

        self.name = name

        # If virustotal is none, initalize total, positives, scans to None
        if self.report.get("virustotal") is None:
            self.total = None
            self.positives = None
            self.scans = None
            return

        # Get total and positives
        self.total = self.report.get("virustotal").get("total")
        self.positives = self.report.get("virustotal").get("positives")
        # Pull all VT normalised results
        self.scans = self.report.get("virustotal").get("scans")


    # Load reports and skip virustotal section
    def load_report_json(self, json_file, name="unknown"):
        """Load JSON formatted malware report. It can handle both a path to
        JSON file and a dictionary object."""
        if isinstance(json_file, str):
            self.json_path = json_file
            with open(json_file, "r") as malware_report:
                try:
                    self.report = json.load(malware_report)
                except ValueError, error:
                    print >> sys.stderr, "Could not load file;", \
                        malware_report, "is not a valid JSON file."
                    print >> sys.stderr, "Exception: %s" % str(error)
                    sys.exit(1)
        elif isinstance(json_file, dict):
            self.report = json_file
        else:
            # Unknown binary format
            print >> sys.stderr, "Could not load the data *", json, "* is of " \
                "unknown type: ", type(json), "."

        self.name = name

        self.total = None


    def label_sample(self, external_labels=None, label_type="family"):
        """Generate label for the loaded sample.
        You can use platform, cve, metatype, type, and family (default)."""
        merged_labels = []

        if external_labels is not None:
            self.label = external_labels
            # print "external_labels is not none"
            # print external_labels
            return

        if external_labels is None and self.scans is not None:
            for vendor in self.scans:
                merged_labels += self.scans[vendor]["normalized"][label_type]
        # elif external_labels is not None and self.scans is None:
        #     merged_labels = external_labels

        if not merged_labels:
            self.label = "none"
            return

        # Get most common label if it has more hits than set threshold
        labels_frequency = collections.Counter(merged_labels)
        top_label, top_label_count = labels_frequency.most_common(1)[0]
        if top_label_count >= self.LABEL_SIGNIFICANCE_COUNT:
                # self.positives >= self.POSITIVE_RATE:
            self.label = top_label.encode("ascii", "ignore")
        else:
            self.label = "none"


    # Add files (reports)
    def update(self, element, location):
        """Insert `element` at given `location`."""
        element_to_update = self.report
        for l in location[:-1]:
            etu = element_to_update.get(l)
            if etu is None:
                element_to_update[l] = {}
                element_to_update = element_to_update.get(l)
            else:
                element_to_update = etu
        element_to_update[location[-1]] = element


    def save_json(self, root_dir):
        """Save JSON stored in the class to a file."""
        with open(root_dir+self.name, "w") as j_file:
            json.dump(self.report, j_file)


    # Extract static and dynamic features
    def extract_features(self):
        """Extract features of the loaded sample."""
        self.extract_features_static()
        self.extract_features_dynamic()


    # Extract static features
    def extract_features_static(self):
        """Extract static features of the loaded sample."""
        self.feature_static_metadata()
        self.feature_static_signature()
        self.feature_static_heuristic()
        self.feature_static_packer()
        self.feature_static_pef()
        self.feature_static_imports()


    # Extract dynamic features
    def extract_features_dynamic(self):
        """Extract dynamic features of the loaded sample."""
        self.feature_dynamic_imports()
        self.feature_dynamic_filesystem()
        self.feature_dynamic_network()
        self.feature_dynamic_registry()
        self.feature_dynamic_windowsapi()


    # Get feasures from static binary
    # Add three features: 
    # size, FileDescription, OriginalFilename
    def feature_static_metadata(self):
        """Create features form extracted binary metadata."""
        # Get binary size
        self.features["size"] = \
            self.report.get("target", {}).get("file", {}).get("size")

        # Get binary timestamp in the UNIX timestamp format
        str_dt = self.report.get("static", {}).get("pe_timestamp")
        ts = None
        if str_dt is not None:
            dt = datetime.datetime.strptime(str_dt, "%Y-%m-%d %H:%M:%S")
            ts = int(time.mktime(dt.timetuple()))
        self.features["timestamp"] = ts

        # ExifTool output
        et_tokens = ["FileDescription", "OriginalFilename"]
        for token in et_tokens:
            self.features[token] = None
        for attr in self.report.get("static", {}).get("pe_versioninfo", []):
            attr_name = attr.get("name")
            if attr_name in et_tokens:
                self.features[attr_name] = attr.get("value")

        # Magic byte
        self.features["magic_byte"] = \
            self.report.get("target", {}).get("file", {}).get("type")


    # Get feasures from static binary
    # Add five features: 
    # signed, Comments, ProductName, 
    # LegalCopyright, InternalName, CompanyName
    def feature_static_signature(self):
        """Create features form binary signature check."""
        # Check availability of digital signature
        self.features["signed"] = \
            bool(self.report.get("static", {}).get("signature", []))

        # ExifTool output
        et_tokens = ["Comments", "ProductName", "LegalCopyright", \
                     "InternalName", "CompanyName"]
        for token in et_tokens:
            self.features[token] = None
        for attr in self.report.get("static", {}).get("pe_versioninfo", []):
            attr_name = attr.get("name")
            if attr_name in et_tokens:
                self.features[attr_name] = attr.get("value")


    # Pass
    def feature_static_heuristic(self):
        """Create features form results return by heuristic tools.
        **Not available for current JSON content.**"""
        pass


    # Get feasures from static binary
    # Add one feature: 
    # packer
    def feature_static_packer(self):
        """Create feature from information returned by packer/cryptor
        detectors."""
        self.features["packer"] = \
            self.report.get("static", {}).get("peid_signatures", None)


    # Get feasures from static binary
    # Add three features: 
    # languages, section_attrs, resource_attrs
    def feature_static_pef(self):
        """Create features from information derived form portable executable
        format."""
        # Get resource languages
        self.features["languages"] = []
        for d in self.report.get("static", {}).get("pe_resources", []):
            lang = d.get("language", False)
            if lang:
                if lang.startswith("LANG_"):
                    lang = lang[5:]
                else:
                    lang = lang
                if lang not in self.features["languages"]:
                    self.features["languages"].append(lang)
            sublang = d.get("sublanguage", False)
            if sublang:
                if sublang.startswith("SUBLANG_"):
                    sublang = sublang[8:]
                else:
                    sublang = sublang
                if sublang not in self.features["languages"]:
                    self.features["languages"].append(sublang)

        # Section and resource attributes
        self.features["section_attrs"] = {}
        for d in self.report.get("static", {}).get("pe_sections", []):
            n = d.get("name")
            e = d.get("entropy")
            if n and d:
                self.features["section_attrs"][n] = e
        self.features["resource_attrs"] = {}
        for d in self.report.get("static", {}).get("pe_resources", []):
            n = d.get("name")
            f = d.get("filetype")
            if n and f:
                self.features["resource_attrs"][n] = f


    # Get feasures from static binary
    # Add one feature: 
    # static_imports
    def feature_static_imports(self):
        """Extract features from static imports like referenced library
        functions."""
        self.features["static_imports"] = {}

        # Static libraries import count
        self.features["static_imports"]["count"] = \
            self.report.get("static", {}).get("imported_dll_count", None)

        # Get all imported libraries
        for d in self.report.get("static", {}).get("pe_imports", []):
            ddl_name = d.get("dll")
            if not ddl_name:
                continue
            self.features["static_imports"][ddl_name] = []
            for i in d.get("imports", []):
                ref = i.get("name")
                if ref is not None:
                    self.features["static_imports"][ddl_name].append(ref)


    # Get feasures from dynamic binary
    # Add three features: 
    # mutex, processes, dynamic_imports
    def feature_dynamic_imports(self):
        """Extract features from dynamic imports, mutexes, and processes."""
        # Get mutexes
        self.features["mutex"] = \
            self.report.get("behavior", {}).get("summary", {}).get("mutex")

        # Get processes names
        self.features["processes"] = []
        for p in self.report.get("behavior", {}).get("processes", []):
            p_name = p.get("process_name")
            if p_name and p_name not in self.features["processes"]:
                self.features["processes"].append(p_name)

        # Get dynamically loaded library names
        self.features["dynamic_imports"] = \
            self.report.get("behavior", {}).get("summary", {})\
            .get("dll_loaded", [])


    # Get feasures from dynamic binary
    # Add one feature: 
    # files_operations
    # =========== which consist of ===========
    # file_read, file_written, file_deleted, file_copied, 
    # file_renamed, files_opened, files_exists, files_failed
    def feature_dynamic_filesystem(self):
        """Extract features from filesystem operations."""
        def flatten_list(structured):
            """Flatten nested list."""
            flat = []
            for i in structured:
                flat += i
            return flat

        # Get file operations and their number
        self.features["file_read"] = \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_read", [])
        self.features["files_read"] = len(self.features["file_read"])
        self.features["file_written"] = \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_written", [])
        self.features["files_written"] = len(self.features["file_written"])
        self.features["file_deleted"] = \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_deleted", [])
        self.features["files_deleted"] = len(self.features["file_deleted"])
        self.features["file_copied"] = flatten_list(\
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_copied", [])
                                                   )
        self.features["files_copied"] = len(\
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_copied", [])
                                            )
        self.features["file_renamed"] = flatten_list(\
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_moved", [])
                                                    )
        self.features["files_renamed"] = len(self.features["file_renamed"])

        # Get other file operations numbers
        self.features["files_opened"] = len(
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_opened", [])
        )
        self.features["files_exists"] = len(
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_exists", [])
        )
        self.features["files_failed"] = len(
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_failed", [])
        )

        # Get total number of unique touched files
        file_operations = \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_read", []) + \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_written", []) + \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_deleted", []) + \
            flatten_list(self.report.get("behavior", {}).get("summary", {})\
            .get("file_copied", [])) + \
            flatten_list(self.report.get("behavior", {}).get("summary", {})\
            .get("file_moved", [])) + \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_recreated", []) + \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_opened", []) + \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_exists", []) + \
            self.report.get("behavior", {}).get("summary", {})\
            .get("file_failed", [])
        # remove duplicates
        self.features["files_operations"] = len(list(set(file_operations)))


    # Get feasures from dynamic binary
    # Add four features: 
    # tcp, udp, dns, http
    def feature_dynamic_network(self):
        """Extract features from network operations."""
        # Get TCP IP addresses
        self.features["tcp"] = []
        for c in self.report.get("network", {}).get("tcp", []):
            c_dst = c.get("dst")
            if c_dst and c_dst not in self.features["tcp"]:
                self.features["tcp"].append(c_dst)

        # Get UDP IPs
        self.features["udp"] = []
        for c in self.report.get("network", {}).get("udp", []):
            c_dst = c.get("dst")
            if c_dst and c_dst not in self.features["udp"]:
                self.features["udp"].append(c_dst)

        # Get DNS queries and responses
        self.features["dns"] = {}
        for c in self.report.get("network", {}).get("dns", []):
            request = c.get("request")
            if request:
                self.features["dns"][request] = []
            else:
                continue

            answers = c.get("answers", [])
            for a in answers:
                a_type = a.get("type")
                a_data = a.get("data")
                if a_type == "A" and a_data:
                    self.features["dns"][request].append(a_data)

        # Get HTTP requests: method, host, port, path
        self.features["http"] = {}
        for c in self.report.get("network", {}).get("http", []):
            c_data = c.get("data")
            if c_data:
                self.features["http"][c_data] = {}
            else:
                continue

            c_method = c.get("method")
            if c_method:
                self.features["http"][c_data]["method"] = c_method
            c_host = c.get("host")
            if c_host:
                self.features["http"][c_data]["host"] = c_host
            c_port = c.get("port")
            if c_port:
                self.features["http"][c_data]["port"] = c_port


    # Get feasures from dynamic binary
    # Add two features: 
    # regkey_written, regkey_deleted
    def feature_dynamic_registry(self):
        """Extract features from registry operations."""
        # Registry written
        self.features["regkey_written"] = \
            self.report.get("behavior", {}).get("summary", {})\
            .get("regkey_written", [])
        # Registry delete
        self.features["regkey_deleted"] = \
            self.report.get("behavior", {}).get("summary", {})\
            .get("regkey_deleted", [])


    # Get feasures from dynamic binary
    # Add one feature: 
    # api_stats
    def feature_dynamic_windowsapi(self):
        """Extract features from Windows API calls sequence."""
        self.features["api_stats"] = {}
        apistats = self.report.get("behavior", {}).get("apistats", {})
        for d in apistats:
            for e in apistats[d]:
                if e in self.features["api_stats"]:
                    self.features["api_stats"][e] += apistats[d][e]
                else:
                    self.features["api_stats"][e] = apistats[d][e]

    # Extract simple features under signatures
    # Name => Description
    def extract_basic_features(self):
        """Extract very basic set of features from *signatures* JSON field.
        These are extracted characteristics of the binary by cuckoo sandbox."""
        if self.basic_features:
            self.basic_features = {}

        for s in self.report.get("signatures", []):
            name = s.get("name", "")
            description = s.get("description", "")
            if name:
                self.basic_features[name] = description
                continue
            if description:
                self.basic_features[hash(description)] = description
