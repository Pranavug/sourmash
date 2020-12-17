from .index import Index


class RevIndex(Index):
    def __init__(self, _signatures=None, filename=None):
        self._signatures = []
        if _signatures:
            self._signatures = list(_signatures)
        self.filename = filename

    def signatures(self):
        return iter(self._signatures)

    def __len__(self):
        return len(self._signatures)

    def insert(self, node):
        self._signatures.append(node)

    def save(self, path):
        from .signature import save_signatures
        with open(path, 'wt') as fp:
            save_signatures(self.signatures(), fp)

    @classmethod
    def load(cls, location):
        from .signature import load_signatures
        si = load_signatures(location)

        lidx = LinearIndex(si, filename=location)
        return lidx

    def select(self, ksize=None, moltype=None):
        def select_sigs(siglist, ksize, moltype):
            for ss in siglist:
                if (ksize is None or ss.minhash.ksize == ksize) and \
                   (moltype is None or ss.minhash.moltype == moltype):
                   yield ss

        siglist=select_sigs(self._signatures, ksize, moltype)
        return LinearIndex(siglist, self.filename)
