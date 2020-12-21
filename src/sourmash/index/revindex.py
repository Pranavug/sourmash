import weakref

from sourmash.index import Index
from sourmash.minhash import MinHash
from sourmash.signature import SourmashSignature
from sourmash._lowlevel import ffi, lib
from sourmash.utils import RustObject, rustcall, decode_str, encode_str


class RevIndex(RustObject, Index):
    __dealloc_func__ = lib.revindex_free

    def __init__(
        self, signature_paths, template=None, threshold=0, queries=None, keep_sigs=False
    ):
        attached_refs = weakref.WeakKeyDictionary()

        search_sigs_ptr = ffi.NULL
        sigs_size = 0
        if signature_paths:
            # get list of rust objects
            collected = []
            for path in signature_paths:
                collected.append(encode_str(path))
            search_sigs_ptr = ffi.new("SourmashStr*[]", collected)
            sigs_size = len(signature_paths)

        queries_ptr = ffi.NULL
        queries_size = 0
        if queries:
            # get list of rust objects
            collected = []
            for obj in queries:
                rv = obj._get_objptr()
                attached_refs[rv] = obj
                collected.append(rv)
            queries_ptr = ffi.new("SourmashSignature*[]", collected)
            queries_size = len(queries)

        template_ptr = ffi.NULL
        if template:
            if isinstance(template, MinHash):
                template_ptr = template._get_objptr()
            else:
                raise ValueError("Template must be a MinHash")

        self._objptr = rustcall(
            lib.revindex_new,
            search_sigs_ptr,
            sigs_size,
            template_ptr,
            threshold,
            queries_ptr,
            queries_size,
            keep_sigs,
        )

    def signatures(self):
        pass

    def __len__(self):
        pass

    def insert(self, node):
        pass

    def save(self, path):
        pass

    @classmethod
    def load(cls, location):
        pass

    def select(self, ksize=None, moltype=None):
        pass

    def search(self, query, *args, **kwargs):
        """Return set of matches with similarity above 'threshold'.

        Results will be sorted by similarity, highest to lowest.

        Optional arguments:
          * do_containment: default False. If True, use Jaccard containment.
          * ignore_abundance: default False. If True, and query signature
            and database support k-mer abundances, ignore those abundances.

        Note, the "best only" hint is ignored by LCA_Database
        """
        if not query.minhash:
            return []

        # check arguments
        if "threshold" not in kwargs:
            raise TypeError("'search' requires 'threshold'")
        threshold = kwargs["threshold"]
        do_containment = kwargs.get("do_containment", False)
        ignore_abundance = kwargs.get("ignore_abundance", False)

        size = ffi.new("uintptr_t *")
        results_ptr = self._methodcall(
            lib.revindex_search,
            query._get_objptr(),
            threshold,
            do_containment,
            ignore_abundance,
            size,
        )

        size = size[0]
        if size == 0:
            return []

        results = []
        for i in range(size):
            match = SearchResult._from_objptr(results_ptr[i])
            if match.score >= threshold:
                results.append((match.score, match.signature, match.filename))

        return results

    def gather(self, query, *args, **kwargs):
        "Return the match with the best Jaccard containment in the database."
        if not query.minhash:
            return []

        threshold_bp = kwargs.get("threshold_bp", 0.0)
        threshold = threshold_bp / (len(query.minhash) * self.scaled)

        results = []
        results_ptr = self._methodcall(
            lib.lcadb_gather, query._get_objptr(), threshold, True, True
        )
        if results_ptr != ffi.NULL:
            match = SearchResult._from_objptr(results_ptr)
            if match.score:
                results.append((match.score, match.signature, match.filename))

        return results


class SearchResult(RustObject):
    __dealloc_func__ = lib.searchresult_free

    @property
    def score(self):
        return self._methodcall(lib.searchresult_score)

    @property
    def signature(self):
        sig_ptr = self._methodcall(lib.searchresult_signature)
        return SourmashSignature._from_objptr(sig_ptr)

    @property
    def filename(self):
        result = decode_str(self._methodcall(lib.searchresult_filename))
        if result == "":
            return None
        return result
