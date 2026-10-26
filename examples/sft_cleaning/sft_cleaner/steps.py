"""Actual DataTrove pipeline blocks; one complete input file is owned by one task."""
from collections import Counter
from pathlib import Path
import time

from datatrove.data import Document
from datatrove.pipeline.base import PipelineStep

from .core import encoded, iter_records, mechanical, normalize, save_new


class SFTReader(PipelineStep):
    name, type = 'Typed SFT reader', 'Reader'

    def __init__(self, inputs, max_bytes):
        super().__init__()
        self.inputs, self.max_bytes = inputs, max_bytes

    def run(self, data=None, rank=0, world_size=1):
        for ordinal, source in enumerate(self.inputs):
            if ordinal % world_size != rank:
                continue
            for index, row in iter_records(source, self.max_bytes):
                if source.get('limit') is not None and index >= source['limit']:
                    break
                envelope = normalize(row, source, index)
                self.stat_update('input_rows')
                yield Document(text='', id=envelope['id'], metadata={'envelope': envelope})


class MechanicalStep(PipelineStep):
    name, type = 'Task-aware mechanical checks', 'Filter'

    def __init__(self, rules):
        super().__init__()
        self.rules = rules

    def run(self, data, rank=0, world_size=1):
        for doc in data:
            value = mechanical(doc.metadata['envelope'], self.rules)
            doc.metadata['envelope'] = value
            self.stat_update(value['state'])
            yield doc


class StageWriter(PipelineStep):
    name, type = 'Auditable SFT stage writer', 'Writer'

    def __init__(self, root):
        super().__init__()
        self.root = str(root)

    def run(self, data, rank=0, world_size=1):
        import hashlib
        root = Path(self.root)
        paths, handles, hashes, sizes = {}, {}, {}, Counter()
        counts, sources, reasons, targets = Counter(), Counter(), Counter(), Counter()
        start = time.monotonic()
        try:
            for state in ('candidate', 'review', 'deferred', 'rejected'):
                final = root / ('part-%05d.%s.jsonl' % (rank, state))
                partial = Path(str(final) + '.partial')
                # Restart only this uncommitted rank; preserve interrupted bytes.
                for old in (partial, final):
                    if old.exists():
                        old.rename(Path(str(old) + '.interrupted-' + str(time.time_ns())))
                paths[state], handles[state] = (partial, final), partial.open('xb')
                hashes[state] = hashlib.sha256()
            for doc in data:
                value = doc.metadata['envelope']
                state = value['state']
                raw = encoded(value)
                handles[state].write(raw)
                hashes[state].update(raw)
                sizes[state] += len(raw)
                counts[state] += 1
                sources[value['source'] + '|' + state] += 1
                targets[state] += sum(value.get('train_turns', []))
                reasons.update(value['issues'])
            for state, f in handles.items():
                f.close()
                paths[state][0].rename(paths[state][1])
            stats_path = root / ('part-%05d.stats.json' % rank)
            if stats_path.exists():
                stats_path.rename(Path(str(stats_path) + '.interrupted-' + str(time.time_ns())))
            save_new(stats_path, {'rows': dict(counts), 'supervised_targets': dict(targets),
                'source_states': dict(sources), 'reasons': dict(reasons),
                'files': {paths[k][1].name: {'bytes': sizes[k], 'sha256': hashes[k].hexdigest()} for k in paths},
                'seconds': round(time.monotonic() - start, 3)})
        finally:
            for f in handles.values():
                f.close()
        yield from ()
