""" TERM DEFINITION
(l, r) : L and R position subwords
stem : stem of Adjective and Verb
ending : suffix, canonical form of ending

stems : set of stem including Adjectives and Verbs
composable_stems : stems that can be compounded with other prefix
    - [] + 하다 : 덕질+하다, 냐옹+하다, 냐옹+하냥
endings : set of ending
pos_l_features : canonical form set of stems (L subwords)
pos_composable_l_features : canonical form set of composable stems (L subwords)
lrgraph : L-R graph including [stem + Ending], Adverbs, 
          and maybe some Noun + Josa
"""

from collections import defaultdict
from collections import namedtuple
from soynlp.hangle import character_is_complete_korean
from soynlp.utils import LRGraph
from soynlp.utils import get_process_memory
from soynlp.utils import EojeolCounter
from soynlp.utils.utils import installpath
from soynlp.lemmatizer import conjugate
from soynlp.lemmatizer import _lemma_candidate
from soynlp.lemmatizer import _conjugate_stem
from ._eomi import EomiExtractor
from ._stem import StemExtractor
from ._adjective_vs_verb import conjugate_as_present
from ._adjective_vs_verb import conjugate_as_imperative
from ._adjective_vs_verb import conjugate_as_pleasure
from ._adjective_vs_verb import rule_classify

Predicator = namedtuple('Predicator', 'frequency lemma')

class PredicatorExtractor:

    def __init__(self, nouns, noun_pos_features=None, adjectives=None, verbs=None,
        eomis=None, extract_eomi=False, extract_stem=False, verbose=True):

        if not noun_pos_features:
            noun_pos_features = self._load_default_noun_pos_features()

        if (adjectives is None) or (verbs is None):
            adjectives, verbs = self._load_default_stems()

        if eomis is None:
            eomis = self._load_default_eomis()

        self._nouns = nouns
        self._noun_pos_features = noun_pos_features
        self._adjective_stems = adjectives
        self._verb_stems = verbs
        self._stems = {stem for stem in adjectives}
        self._stems = self._stems.union(verbs)
        self._eomis = eomis
        self.verbose = verbose
        self.extract_eomi = extract_eomi
        self.extract_stem = extract_stem

        self._stem_surfaces = self._transform_stem_as_surfaces()
        self.lrgraph = None

    def _load_default_noun_pos_features(self):
        path = '%s/trained_models/noun_predictor_ver2_pos' % installpath
        with open(path, encoding='utf-8') as f:
            pos_features = {word.split()[0] for word in f}
        return pos_features

    def _load_default_stems(self, min_frequency=2):
        def load(path):
            stems = set()
            with open(path, encoding='utf-8') as f:
                for line in f:
                    word, frequency = line.split()
                    if int(frequency) < min_frequency:
                        continue
                    stems.add(word)
            return stems

        dirs = '%s/lemmatizer/dictionary/default/Stem' % installpath
        adjectives = load('%s/Adjective.txt' % dirs)
        verbs = load('%s/Verb.txt' % dirs)
        return adjectives, verbs

    def _load_default_eomis(self, min_frequency=20):
        path = '%s/lemmatizer/dictionary/default/Eomi/Eomi.txt' % installpath
        eomis = set()
        with open(path, encoding='utf-8') as f:
            for line in f:
                word, frequency = line.split()
                if int(frequency) < min_frequency:
                    continue
                eomis.add(word)
        return eomis

    def _transform_stem_as_surfaces(self):
        surfaces = set()
        for stem in self._stems:
            try:
                for l in _conjugate_stem(stem):
                    surfaces.add(l)
            except Exception as e:
                print('Exception stem = {}, {}'.format(stem, e))
                continue
        return surfaces

    def _print(self, message, replace=False, newline=True):
        header = '[Predicator Extractor]'
        if replace:
            print('\r{} {}'.format(header, message),
                  end='\n' if newline else '', flush=True)
        else:
            print('{} {}'.format(header, message),
                  end='\n' if newline else '', flush=True)

    @property
    def is_trained(self):
        return self.lrgraph

    def train_extract(self, sentences_or_lrgraph, min_eojeol_frequency=2,
        filtering_checkpoint=100000, candidates=None,
        min_predicator_frequency=10, reset_lrgraph=True,
        # Eomi extractor
        min_num_of_features=5, min_eomi_score=0.3, min_eomi_frequency=1,
        # Stem extractor
        min_num_of_unique_R_char=10, min_entropy_of_R_char=0.5,
        min_entropy_of_R=1.5, min_stem_score=0.7, min_stem_frequency=100):

        self.train(sentences_or_lrgraph, min_eojeol_frequency, filtering_checkpoint)

        predicators = self.extract(
            candidates, min_predicator_frequency, reset_lrgraph,
            min_num_of_features, min_eomi_score, min_eomi_frequency,
            min_num_of_unique_R_char, min_entropy_of_R_char,
            min_entropy_of_R, min_stem_score, min_stem_frequency)
        return predicators

    def train(self, sentences_or_lrgraph, min_eojeol_frequency=2,
        filtering_checkpoint=100000):

        if isinstance(sentences_or_lrgraph, LRGraph):
            self._train_with_lrgraph(sentences_or_lrgraph)
        else:
            self._train_with_sentences(sentences_or_lrgraph,
                min_eojeol_frequency, filtering_checkpoint)

    def _train_with_lrgraph(self, lrgraph):
        counter = {}
        for l, rdict in lrgraph._lr.items():
            for r, count in rdict.items():
                counter[l+r] = count

        self._num_of_eojeols = sum(counter.values())
        self._num_of_covered_eojeols = 0
        self.lrgraph = lrgraph

    def _train_with_sentences(self, sentences, min_eojeol_frequency=2,
        filtering_checkpoint=100000):

        check = filtering_checkpoint > 0

        if self.verbose:
            message = 'counting eojeols'
            self._print(message, replace=False, newline=False)

        # Eojeol counting
        counter = {}

        def contains_noun(eojeol, n):
            for e in range(2, n + 1):
                if eojeol[:e] in self._nouns:
                    return True
            return False

        for i_sent, sent in enumerate(sentences):

            if check and i_sent > 0 and i_sent % filtering_checkpoint == 0:
                counter = {
                    eojeol:count for eojeol, count in counter.items()
                    if count >= min_eojeol_frequency
                }

            if self.verbose and i_sent % 100000 == 99999:
                message = 'n eojeol = {} from {} sents. mem={} Gb{}'.format(
                    len(counter), i_sent + 1, '%.3f' % get_process_memory(), ' '*20)
                self._print(message, replace=True, newline=False)

            for eojeol in sent.split():

                n = len(eojeol)

                if n <= 1 or contains_noun(eojeol, n):
                    continue

                counter[eojeol] = counter.get(eojeol, 0) + 1

        if self.verbose:
            message = 'counting eojeols was done. {} eojeols, mem={} Gb{}'.format(
                len(counter), '%.3f' % get_process_memory(), ' '*20)
            self._print(message, replace=True, newline=True)

        counter = {
            eojeol:count for eojeol, count in counter.items()
            if count >= min_eojeol_frequency
        }

        self._num_of_eojeols = sum(counter.values())
        self._num_of_covered_eojeols = 0

        if self.verbose:
            message = 'complete eojeol counter -> lr graph'
            self._print(message, replace=False, newline=True)

        self.lrgraph = EojeolCounter()._to_lrgraph(
            counter,
            l_max_length=10,
            r_max_length=9
        )

        if self.verbose:
            message = 'has been trained. mem={} Gb'.format(
                '%.3f' % get_process_memory())
            self._print(message, replace=False, newline=True)

    def extract(self, candidates=None,
        min_predicator_frequency=10, reset_lrgraph=True,
        # Eomi extractor
        min_num_of_features=5, min_eomi_score=0.3, min_eomi_frequency=1,
        # Stem extractor
        min_num_of_unique_R_char=10, min_entropy_of_R_char=0.5,
        min_entropy_of_R=1.5, min_stem_score=0.7, min_stem_frequency=100):

        # reset covered eojeol count
        self._num_of_covered_eojeols = 0

        # TODO link parameters
        if self.extract_eomi:
            self._extract_eomi(min_num_of_features,
                min_eomi_score, min_eomi_frequency)

        # TODO link parameters
        if self.extract_stem:
            self._extract_stem(min_num_of_unique_R_char,
                min_entropy_of_R_char, min_entropy_of_R,
                min_stem_score, min_stem_frequency)

        predicators = self._extract_predicator(
            candidates, min_predicator_frequency, reset_lrgraph)

        adjectives, verbs = self._separate_adjective_verb(predicators)

        return adjectives, verbs

    def _extract_eomi(self, min_num_of_features=5, min_eomi_score=0.3, min_eomi_frequency=1):

        eomi_extractor = EomiExtractor(
            lrgraph = self.lrgraph,
            stems = self._stems,
            nouns = self._nouns,
            min_num_of_features = min_num_of_features,
            verbose = self.verbose,
            logpath = None
        )

        extracted_eomis = eomi_extractor.extract(
            condition=None,
            min_eomi_score = min_eomi_score,
            min_frequency = min_eomi_frequency,
            reset_lrgraph=True
        )

        extracted_eomis = {eomi for eomi in extracted_eomis if not (eomi in self._eomis)}
        # TODO: update adjective_stems & verb_stems ?
        self._eomis.update(extracted_eomis)

        if self.verbose:
            message = '{} eomis have been extracted'.format(len(extracted_eomis))
            self._print(message, replace=False, newline=True)

    def _extract_stem(self, min_num_of_unique_R_char=10, min_entropy_of_R_char=0.5,
        min_entropy_of_R=1.5, min_stem_score=0.7, min_stem_frequency=100):

        stem_extractor = StemExtractor(
            lrgraph = self.lrgraph,
            stems = self._stems,
            eomis = self._eomis,
            min_num_of_unique_R_char = min_num_of_unique_R_char,
            min_entropy_of_R_char = min_entropy_of_R_char,
            min_entropy_of_R = min_entropy_of_R
        )

        extracted_stems = stem_extractor.extract(
            L_ignore=None,
            min_stem_score = min_stem_score,
            min_stem_frequency = min_stem_frequency
        )

        extracted_stems = {stem for stem in extracted_stems if not (stem in self._stems)}
        self._stems.update(extracted_stems)

        if self.verbose:
            message = '{} stems have been extracted'.format(len(extracted_stems))
            self._print(message, replace=False, newline=True)

    def _extract_predicator(self, eojeols=None, min_frequency=1, reset_lrgraph=True):
        lemmas = self._as_lemma_candidates(eojeols, min_frequency)

        # TODO
        # evaluation

        if self.verbose:
            message = '{} predicators are extracted'.format(len(lemmas))
            self._print(message, replace=True, newline=True)

        return lemmas

    def _as_lemma_candidates(self, eojeols=None,  min_frequency=10):

        def all_character_are_complete_korean(s):
            for c in s:
                if not character_is_complete_korean(c):
                    return False
            return True

        if not eojeols:
            eojeols = {l:rdict.get('', 0) for l, rdict in self.lrgraph._lr.items()}
            eojeols = {eojeol:count for eojeol, count in eojeols.items()
                       if (count > min_frequency) and all_character_are_complete_korean(eojeol)}

        n_eojeols = len(eojeols)
        lemmas = {}

        for i_eojeol, eojeol in enumerate(eojeols):

            if self.verbose and i_eojeol % 5000 == 0:
                perc = '%.3f'% (100 * i_eojeol / n_eojeols)
                message = 'lemma candidates ... {} %'.format(perc)
                self._print(message, replace=True, newline=False)

            n = len(eojeol)
            lemma_candidates = set()

            for i in range(1, n+1):
                l, r = eojeol[:i], eojeol[i:]
                for stem, eomi in _lemma_candidate(l, r):
                    if (stem in self._stems) and (eomi in self._eomis):
                        lemma_candidates.add((stem, eomi))

            lemma_candidates_ = set()
            for stem, eomi in lemma_candidates:
                if eojeol in conjugate(stem, eomi):
                    lemma_candidates_.add((stem, eomi))

            if lemma_candidates_:
                count = eojeols[eojeol]
                lemmas[eojeol] = Predicator(count, lemma_candidates_)
                self._num_of_covered_eojeols += count

        if self.verbose:
            message = 'lemma candidating was done. {} % eojeols are covered'.format(
                '%.3f' % (100 * self._num_of_covered_eojeols / self._num_of_eojeols))
            self._print(message, replace=True, newline=True)

        return lemmas

    def _separate_adjective_verb(self, predicators, num_threshold=3):
        adjectives = {}
        verbs = {}

        # proportion
        for word, predicator in predicators.items():
            frequency = predicator.frequency
            lemmas = predicator.lemma
            adj = set()
            v = set()
            for lemma in lemmas:
                # dictionary first, verb preference
                if lemma[0] in self._verb_stems:
                    v.add(lemma)
                    continue
                if lemma[0] in self._adjective_stems:
                    adj.add(lemma)
                    continue

                # others are extracted stems
                # rule based classifier, verb preference
                answer = rule_classify(lemma[0])
                if answer is 'Verb':
                    v.add(lemma)
                    continue
                if answer is 'Adjective':
                    adj.add(lemma)
                    continue

                # surfacial form test
                surfaces = conjugate_as_present(lemma[0])
                surfaces.update(conjugate_as_imperative(lemma[0]))
                surfaces.update(conjugate_as_pleasure(lemma[0]))
                surfaces = {surface for surface in surfaces
                            if surface in predicators}

                if len(surfaces) <= 1:
                    adj.add(lemma)
                else:
                    v.add(lemma)

            if adj:
                adjectives[word] = Predicator(frequency, adj)
            if v:
                verbs[word] = Predicator(frequency, v)

        return adjectives, verbs
