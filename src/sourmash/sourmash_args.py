"""
Utility functions for sourmash CLI commands.
"""
import sys
import os
import argparse
import itertools
from enum import Enum
import traceback

import screed

from sourmash.sbtmh import load_sbt_index
from sourmash.lca.lca_db import load_single_database
import sourmash.exceptions

from . import signature
from .logging import notify, error, debug_literal

from .index import LinearIndex, MultiIndex
from . import signature as sig
from .sbt import SBT
from .sbtmh import SigLeaf
from .lca import LCA_Database
import sourmash

DEFAULT_LOAD_K = 31


def get_moltype(sig, require=False):
    mh = sig.minhash
    if mh.moltype in ('DNA', 'dayhoff', 'hp', 'protein'):
        moltype = mh.moltype
    else:
        raise ValueError('unknown molecule type for sig {}'.format(sig))

    return moltype


def calculate_moltype(args, default=None):
    moltype = default

    n = 0
    if args.dna:
        moltype = 'DNA'
        n += 1
    if args.dayhoff:
        moltype = 'dayhoff'
        n += 1
    if args.hp:
        moltype = 'hp'
        n += 1
    if args.protein:
        moltype = 'protein'
        n += 1

    if n > 1:
        error("cannot specify more than one of --dna/--rna/--protein/--hp/--dayhoff")
        sys.exit(-1)

    return moltype


def load_query_signature(filename, ksize, select_moltype, select_md5=None):
    """Load a single signature to use as a query.

    Uses load_file_as_signatures underneath, so can load from collections
    and indexed databases.
    """
    try:
        sl = load_file_as_signatures(filename, ksize=ksize,
                                     select_moltype=select_moltype)
        sl = list(sl)
    except (OSError, ValueError):
        error(f"Cannot open query file '{filename}'")
        sys.exit(-1)

    if len(sl) and select_md5:
        found_sig = None
        for sig in sl:
            sig_md5 = sig.md5sum()
            if sig_md5.startswith(select_md5.lower()):
                # make sure we pick only one --
                if found_sig is not None:
                    error(f"Error! Multiple signatures start with md5 '{select_md5}'")
                    error("Please use a longer --md5 selector.")
                    sys.exit(-1)
                else:
                    found_sig = sig

            sl = [found_sig]

    if len(sl) and ksize is None:
        ksizes = set([ ss.minhash.ksize for ss in sl ])
        if len(ksizes) == 1:
            ksize = ksizes.pop()
            sl = [ ss for ss in sl if ss.minhash.ksize == ksize ]
            notify(f'select query k={ksize} automatically.')
        elif DEFAULT_LOAD_K in ksizes:
            sl = [ ss for ss in sl if ss.minhash.ksize == DEFAULT_LOAD_K ]
            notify(f'selecting default query k={DEFAULT_LOAD_K}.')
    elif ksize:
        notify(f'selecting specified query k={ksize}')

    if len(sl) != 1:
        error(f"When loading query from '{filename}'", filename)
        error(f'{len(sl)} signatures matching ksize and molecule type;')
        error('need exactly one. Specify --ksize or --dna, --rna, or --protein.')
        sys.exit(-1)

    return sl[0]


def _check_suffix(filename, endings):
    for ending in endings:
        if filename.endswith(ending):
            return True
    return False


def traverse_find_sigs(filenames, yield_all_files=False):
    """Find all .sig and .sig.gz files in & beneath 'filenames'.

    By default, this function returns files with .sig and .sig.gz extensions.
    If 'yield_all_files' is True, this will return _all_ files
    (but not directories).
    """
    endings = ('.sig', '.sig.gz')
    for filename in filenames:
        # check for files in filenames:
        if os.path.isfile(filename):
            if yield_all_files or _check_suffix(filename, endings):
                yield filename

        # filename is a directory -- traverse beneath!
        elif os.path.isdir(filename):
            for root, dirs, files in os.walk(filename):
                for name in files:
                    fullname = os.path.join(root, name)
                    if yield_all_files or _check_suffix(fullname, endings):
                        yield fullname


def _check_signatures_are_compatible(query, subject):
    # is one scaled, and the other not? cannot do search
    if query.minhash.scaled and not subject.minhash.scaled or \
       not query.minhash.scaled and subject.minhash.scaled:
       error("signature {} and {} are incompatible - cannot compare.",
             query, subject)
       if query.minhash.scaled:
           error(f"{query} was calculated with --scaled, {subject} was not.")
       if subject.minhash.scaled:
           error(f"{subject} was calculated with --scaled, {query} was not.")
       return 0

    return 1


def check_tree_is_compatible(treename, tree, query, is_similarity_query):
    # get a minhash from the tree
    leaf = next(iter(tree.leaves()))
    tree_mh = leaf.data.minhash

    query_mh = query.minhash

    if tree_mh.ksize != query_mh.ksize:
        error(f"ksize on tree '{treename}' is {tree_mh.ksize};")
        error(f"this is different from query ksize of {query_mh.ksize}.")
        return 0

    # is one scaled, and the other not? cannot do search.
    if (tree_mh.scaled and not query_mh.scaled) or \
       (query_mh.scaled and not tree_mh.scaled):
        error(f"for tree '{treename}', tree and query are incompatible for search.")
        if tree_mh.scaled:
            error("tree was calculated with scaled, query was not.")
        else:
            error("query was calculated with scaled, tree was not.")
        return 0

    # are the scaled values incompatible? cannot downsample tree for similarity
    if tree_mh.scaled and tree_mh.scaled < query_mh.scaled and \
      is_similarity_query:
        error(f"for tree '{treename}', scaled value is smaller than query.")
        error(f"tree scaled: {tree_mh.scaled}; query scaled: {query_mh.scaled}. Cannot do similarity search.")
        return 0

    return 1


def check_lca_db_is_compatible(filename, db, query):
    query_mh = query.minhash
    if db.ksize != query_mh.ksize:
        error(f"ksize on db '{filename}' is {db.ksize};")
        error(f"this is different from query ksize of {query_mh.ksize}.")
        return 0

    return 1


def load_dbs_and_sigs(filenames, query, is_similarity_query, *, cache_size=None):
    """
    Load one or more SBTs, LCAs, and/or signatures.

    Check for compatibility with query.

    This is basically a user-focused wrapping of _load_databases.

    CTB: this can be refactored into a more generic function with 'filter'.
    """
    query_ksize = query.minhash.ksize
    query_moltype = get_moltype(query)

    containment = True
    if is_similarity_query:
        containment = False

    n_signatures = 0
    n_databases = 0
    databases = []
    for filename in filenames:
        notify(f'loading from {filename}...', end='\r')

        try:
            db, dbtype = _load_database(filename, False, cache_size=cache_size)
        except Exception as e:
            notify(str(e))
            sys.exit(-1)

        try:
            db = db.select(moltype=query_moltype,
                           ksize=query_ksize,
                           num=query.minhash.num,
                           scaled=query.minhash.scaled,
                           containment=containment)
        except ValueError as exc:
            notify(f"ERROR: cannot use '{filename}' for this query.")
            notify(str(exc))
            sys.exit(-1)

        if not db:
            notify(f"no compatible signatures found in '{filename}'")
            sys.exit(-1)

        databases.append(db)

        if 0:
            # are we collecting signatures from an SBT?
            if dbtype == DatabaseType.SBT:
                if not check_tree_is_compatible(filename, db, query,
                                                is_similarity_query):
                    sys.exit(-1)

                databases.append(db)
                notify(f'loaded SBT {filename}', end='\r')
                n_databases += 1

            # or an LCA?
            elif dbtype == DatabaseType.LCA:
                if not check_lca_db_is_compatible(filename, db, query):
                    sys.exit(-1)

                notify(f'loaded LCA {filename}', end='\r')
                n_databases += 1

                databases.append(db)

            # or a mixed collection of signatures?
            elif dbtype == DatabaseType.SIGLIST:
                db = db.select(moltype=query_moltype, ksize=query_ksize)
                filter_fn = lambda s: _check_signatures_are_compatible(query, s)
                db = db.filter(filter_fn)

                if not db:
                    notify(f"no compatible signatures found in '{filename}'")
                    sys.exit(-1)

                databases.append(db)

                notify(f'loaded {len(db)} signatures from {filename}', end='\r')
                n_signatures += len(db)

            # unknown!?
            else:
                raise ValueError(f"unknown dbtype {dbtype}") # CTB check me.

        # END for loop


    notify(' '*79, end='\r')
    if n_signatures and n_databases:
        notify(f'loaded {n_signatures} signatures and {n_databases} databases total.')
    elif n_signatures:
        notify(f'loaded {n_signatures} signatures.')
    elif n_databases:
        notify(f'loaded {n_databases} databases.')


    if not databases:
        notify('** ERROR: no signatures or databases loaded?')
        sys.exit(-1)

    if databases:
        print('')

    return databases


class DatabaseType(Enum):
    SIGLIST = 1
    SBT = 2
    LCA = 3


def _load_stdin(filename, **kwargs):
    "Load collection from .sig file streamed in via stdin"
    db = None
    if filename == '-':
        db = LinearIndex.load(sys.stdin)

    return (db, DatabaseType.SIGLIST)


def _multiindex_load_from_file_list(filename, **kwargs):
    "Load collection from a list of signature/database files"
    db = MultiIndex.load_from_file_list(filename)

    return (db, DatabaseType.SIGLIST)


def _multiindex_load_from_path(filename, **kwargs):
    "Load collection from a directory."
    traverse_yield_all = kwargs['traverse_yield_all']
    db = MultiIndex.load_from_path(filename, traverse_yield_all)

    return (db, DatabaseType.SIGLIST)


def _load_sigfile(filename, **kwargs):
    "Load collection from a signature JSON file"
    try:
        db = LinearIndex.load(filename)
    except sourmash.exceptions.SourmashError as exc:
        raise ValueError(exc)

    return (db, DatabaseType.SIGLIST)


def _load_sbt(filename, **kwargs):
    "Load collection from an SBT."
    cache_size = kwargs.get('cache_size')

    try:
        db = load_sbt_index(filename, cache_size=cache_size)
    except FileNotFoundError as exc:
        raise ValueError(exc)

    return (db, DatabaseType.SBT)


def _load_revindex(filename, **kwargs):
    "Load collection from an LCA database/reverse index."
    db, _, _ = load_single_database(filename)
    return (db, DatabaseType.LCA)


# all loader functions, in order.
_loader_functions = [
    ("load from stdin", _load_stdin),
    ("load from directory", _multiindex_load_from_path),
    ("load from sig file", _load_sigfile),
    ("load from file list", _multiindex_load_from_file_list),
    ("load SBT", _load_sbt),
    ("load revindex", _load_revindex),
    ]


def _load_database(filename, traverse_yield_all, *, cache_size=None):
    """Load file as a database - list of signatures, LCA, SBT, etc.

    Return (db, dbtype), where dbtype is a DatabaseType enum.

    This is an internal function used by other functions in sourmash_args.
    """
    loaded = False
    dbtype = None

    # iterate through loader functions, trying them all. Catch ValueError
    # but nothing else.
    for (desc, load_fn) in _loader_functions:
        try:
            debug_literal(f"_load_databases: trying loader fn {desc}")
            db, dbtype = load_fn(filename,
                                 traverse_yield_all=traverse_yield_all,
                                 cache_size=cache_size)
        except ValueError as exc:
            debug_literal(f"_load_databases: FAIL on fn {desc}.")
            debug_literal(traceback.format_exc())

        if db:
            loaded = True
            break

    # check to see if it's a FASTA/FASTQ record (i.e. screed loadable)
    # so we can provide a better error message to users.
    if not loaded:
        successful_screed_load = False
        it = None
        try:
            # CTB: could be kind of time consuming for a big record, but at the
            # moment screed doesn't expose format detection cleanly.
            with screed.open(filename) as it:
                record = next(iter(it))
            successful_screed_load = True
        except:
            pass

        if successful_screed_load:
            raise ValueError(f"Error while reading signatures from '{filename}' - got sequences instead! Is this a FASTA/FASTQ file?")

    if not loaded:
        raise ValueError(f"Error while reading signatures from '{filename}'.")

    if loaded:                  # this is a bit redundant but safe > sorry
        assert db

    return db, dbtype


def load_file_as_index(filename, yield_all_files=False):
    """Load 'filename' as a database; generic database loader.

    If 'filename' contains an SBT or LCA indexed database, will return
    the appropriate objects.

    If 'filename' is a JSON file containing one or more signatures, will
    return an Index object containing those signatures.

    If 'filename' is a directory, will load *.sig underneath
    this directory into an Index object. If yield_all_files=True, will
    attempt to load all files.
    """
    db, dbtype = _load_database(filename, yield_all_files)
    return db


def load_file_as_signatures(filename, select_moltype=None, ksize=None,
                            yield_all_files=False,
                            progress=None):
    """Load 'filename' as a collection of signatures. Return an iterable.

    If 'filename' contains an SBT or LCA indexed database, will return
    a signatures() generator.

    If 'filename' is a JSON file containing one or more signatures, will
    return a list of those signatures.

    If 'filename' is a directory, will load *.sig
    underneath this directory into a list of signatures. If
    yield_all_files=True, will attempt to load all files.

    Applies selector function if select_moltype and/or ksize are given.
    """
    if progress:
        progress.notify(filename)

    db, dbtype = _load_database(filename, yield_all_files)
    db = db.select(moltype=select_moltype, ksize=ksize)
    loader = db.signatures()

    if progress:
        return progress.start_file(filename, loader)
    else:
        return loader


def load_file_list_of_signatures(filename):
    "Load a list-of-files text file."
    try:
        with open(filename, 'rt') as fp:
            file_list = [ x.rstrip('\r\n') for x in fp ]

        if not os.path.exists(file_list[0]):
            raise ValueError("first element of list-of-files does not exist")
    except OSError:
        raise ValueError(f"cannot open file '{filename}'")
    except UnicodeDecodeError:
        raise ValueError(f"cannot parse file '{filename}' as list of filenames")

    return file_list


class FileOutput(object):
    """A context manager for file outputs that handles sys.stdout gracefully.

    Usage:

       with FileOutput(filename, mode) as fp:
          ...

    does what you'd expect, but it handles the situation where 'filename'
    is '-' or None. This makes it nicely compatible with argparse usage,
    e.g.

    p = argparse.ArgumentParser()
    p.add_argument('--output')
    args = p.parse_args()
    ...
    with FileOutput(args.output, 'wt') as fp:
       ...

    will properly handle no argument or '-' as sys.stdout.
    """
    def __init__(self, filename, mode='wt', newline=None):
        self.filename = filename
        self.mode = mode
        self.fp = None
        self.newline = newline

    def open(self):
        if self.filename == '-' or self.filename is None:
            return sys.stdout
        self.fp = open(self.filename, self.mode, newline=self.newline)
        return self.fp

    def __enter__(self):
        return self.open()

    def __exit__(self, type, value, traceback):
        # do we need to handle exceptions here?
        if self.fp:
            self.fp.close()

        return False

class FileOutputCSV(FileOutput):
    """A context manager for CSV file outputs.

    Usage:

       with FileOutputCSV(filename) as fp:
          ...

    does what you'd expect, but it handles the situation where 'filename'
    is '-' or None. This makes it nicely compatible with argparse usage,
    e.g.

    p = argparse.ArgumentParser()
    p.add_argument('--output')
    args = p.parse_args()
    ...
    with FileOutputCSV(args.output) as w:
       ...

    will properly handle no argument or '-' as sys.stdout.
    """
    def __init__(self, filename):
        self.filename = filename
        self.fp = None

    def open(self):
        if self.filename == '-' or self.filename is None:
            return sys.stdout
        self.fp = open(self.filename, 'w', newline='')
        return self.fp


class SignatureLoadingProgress(object):
    """A wrapper for signature loading progress reporting.

    Instantiate this class once, and then pass it to load_file_as_signatures
    with progress=<obj>.

    Alternatively, call obj.start_file(filename, iter) each time you
    start loading signatures from a new file via iter.

    You can optionally notify of reading a file with `.notify(filename)`.
    """
    def __init__(self, reporting_interval=10):
        self.n_sig = 0
        self.interval = reporting_interval
        self.screen_width = 79

    def short_notify(self, msg_template, *args, **kwargs):
        """Shorten the notification message so that it fits on one line.

        Good for repeating notifications with end='\r' especially...
        """

        msg = msg_template.format(*args, **kwargs)
        end = kwargs.get('end', '\n')
        w = self.screen_width

        if len(msg) > w:
            truncate_len = len(msg) - w + 3
            msg = '<<<' + msg[truncate_len:]

        notify(msg, end=end)

    def notify(self, filename):
        self.short_notify("...reading from file '{}'",
                          filename, end='\r')

    def start_file(self, filename, loader):
        n_this = 0
        n_before = self.n_sig

        try:
            for result in loader:
                # track n from this file, as well as total n
                n_this += 1
                n_total = n_before + n_this
                if n_this and n_total % self.interval == 0:
                    self.short_notify("...loading from '{}' / {} sigs total",
                                      filename, n_total, end='\r')

                yield result
        except KeyboardInterrupt:
            # might as well nicely handle CTRL-C while we're at it!
            notify('\n(CTRL-C received! quitting.)')
            sys.exit(-1)
        finally:
            self.n_sig += n_this

        self.short_notify("loaded {} sigs from '{}'", n_this, filename)
