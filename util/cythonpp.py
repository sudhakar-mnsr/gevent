#!/usr/bin/env python
# Copyright (C) 2011-2012 Denis Bilenko (http://denisbilenko.com)
# Copyright (C) 2015-2016 gevent contributors
from __future__ import print_function
import sys
import os
import os.path
import re
import traceback
import datetime
import difflib
from hashlib import md5
from itertools import combinations, product
import subprocess
import multiprocessing
import tempfile
import shutil

import threading

class Thread(threading.Thread):
    value = None

    def run(self):
        target = getattr(self, '_target', None) # Py3
        if target is None:
            target = getattr(self, '_Thread__target')
            args = getattr(self, '_Thread__args')
        else:
            args = self._args
        self.value = target(*args)

do_exec = None
if sys.version_info >= (3, 0):
    exec("def do_exec(co, loc): exec(co, loc)\n")
else:
    exec("def do_exec(co, loc): exec co in loc\n")


CYTHON = os.environ.get('CYTHON') or 'cython'
DEBUG = os.environ.get('CYTHONPP_DEBUG', False)
WRITE_OUTPUT = False

if os.getenv('READTHEDOCS'):
    # Sometimes RTD fails to put our virtualenv bin directory
    # on the PATH, meaning we can't run cython. Fix that.
    new_path = os.environ['PATH'] + os.pathsep + os.path.dirname(sys.executable)
    os.environ['PATH'] = new_path

# Parameter name in macros must match this regex:
param_name_re = re.compile(r'^[a-zA-Z_]\w*$')

# First line of a definition of a new macro:
define_re = re.compile(r'^#define\s+([a-zA-Z_]\w*)(\((?:[^,)]+,)*[^,)]+\))?\s+(.*)$')


# cython header:
cython_header_re = re.compile(r'^/\* (generated by cython [^\s*]+)[^*]+\*/$', re.I)
#assert cython_header_re.match('/* Generated by Cython 0.21.1 */').group(1) == 'Generated by Cython 0.21.1'
#assert cython_header_re.match('/* Generated by Cython 0.19 on 55-555-555 */').group(1) == 'Generated by Cython 0.19'

class Configuration(frozenset):
    """
    A set of CPP conditions that apply to a given sequence
    of lines. Sometimes referred to as a "tag".

    Configurations are iterated in sorted order for consistency
    across runs.
    """
    __slots__ = ('_sorted',)
    _cache = {}

    def __new__(cls, iterable):
        sorted_iterable = tuple(sorted(frozenset(iterable)))
        if sorted_iterable not in cls._cache:
            if not all(isinstance(x, Condition) for x in sorted_iterable):
                raise TypeError("Must be iterable of conditions")
            if not sorted_iterable:
                raise TypeError("Empty configurations not allowed")
            self = frozenset.__new__(cls, sorted_iterable)
            self._sorted = sorted_iterable
            cls._cache[sorted_iterable] = self

        return cls._cache[sorted_iterable]

    def union(self, other):
        return Configuration(frozenset.union(self, other))

    def __add__(self, conditions):
        return self.union(conditions)

    def difference(self, other):
        return Configuration(frozenset.difference(self, other))

    def __sub__(self, other):
        return self.difference(other)

    def __iter__(self):
        return iter(self._sorted)

    def format_tag(self):
        return ' && '.join([x.format_cond() for x in self])

    def __repr__(self):
        return "Configuration({" + ', '.join((repr(x) for x in self)) + '})'

    @property
    def all_directives(self):
        "All the directives in the conditions of this configuration"
        return set(x.directive for x in self)

    def is_impossible(self):
        """
        Return whether the configuration (a Configuration) contradicts itself.
        """
        conds = {}
        for cond_name, cond_setting in self:
            if cond_name in conds:
                if conds.get(cond_name) != cond_setting:
                    return True
            conds[cond_name] = cond_setting

    def is_condition_true(self, directive):
        if directive.startswith('#if '):
            parameter = directive.split(' ', 1)[1]
        elif directive.startswith('#ifdef '):
            parameter = directive.split(' ', 1)[1]
            parameter = 'defined(%s)' % parameter
        else:
            raise AssertionError('Invalid directive: %r' % directive)
        cond = (parameter, True)
        return cond in self

    def attach_tags(self, text):
        result = [x for x in text.split('\n')]
        if result and not result[-1]:
            del result[-1]
        return [Str(x + '\n', self) for x in result]

    @classmethod
    def get_configurations(cls, filename):
        """
        Returns a set of Configuration objects representing
        the configurations seen in the file.
        """
        conditions = set()
        condition_stack = []
        linecount = 0
        match_condition = Condition.match_condition
        with open(filename) as f:
            for line in f:
                linecount += 1
                try:
                    m = match_condition(line)
                    if m is None:
                        if condition_stack: # added
                            conditions.add(cls(condition_stack))
                        continue

                    split = m.group(1).strip().split(' ', 1)
                    directive = split[0].strip()
                    if len(split) == 1:
                        parameter = None
                        assert directive in ('else', 'endif'), directive
                    else:
                        parameter = split[1].strip()
                        assert directive in ('if', 'ifdef'), directive

                    if directive == 'ifdef':
                        directive = 'if'
                        parameter = 'defined(%s)' % parameter

                    if directive == 'if':
                        condition_stack.append(Condition(parameter, True))
                    elif directive == 'else':
                        if not condition_stack:
                            raise SyntaxError('Unexpected "#else"')
                        last_cond, true = condition_stack.pop()
                        assert true is True, true
                        condition_stack.append(Condition(last_cond, not true))
                    elif directive == 'endif':
                        if not condition_stack:
                            raise SyntaxError('Unexpected "#endif"')
                        condition_stack.pop()
                    else:
                        raise AssertionError('Internal error')
                except BaseException as ex:
                    log('%s:%s: %s', filename, linecount, ex)
                    if isinstance(ex, SyntaxError):
                        sys.exit(1)
                    else:
                        raise
        dbg("Found conditions %s", conditions)
        return conditions


    @classmethod
    def get_permutations_of_configurations(cls, items):
        """
        Returns a set of Configuration objects representing all the
        possible permutations of the given list of configuration
        objects. Impossible configurations are excluded.
        """
        def flattened(tuple_of_configurations):
            # product() produces a list of tuples. Each
            # item in the tuple is a different configuration object.
            set_of_configurations = set(tuple_of_configurations)
            sorted_set_of_configurations = sorted(set_of_configurations)
            conditions = []
            for configuration in sorted_set_of_configurations:
                for condition in configuration:
                    conditions.append(condition)
            return cls(conditions)

        flattened_configurations = (flattened(x) for x in product(items, repeat=len(items)))
        possible_configurations = set((x for x in flattened_configurations if not x.is_impossible()))

        return possible_configurations

    @classmethod
    def get_permutations_of_configurations_in_file(cls, filename):
        """
        Returns a sorted list of unique configurations possible in the given
        file.
        """
        return sorted(cls.get_permutations_of_configurations(cls.get_configurations(filename)))

    @classmethod
    def get_complete_configurations(cls, filename):
        """
        Return a sorted list of the set of unique configurations possible
        in the given file; each configuration will have the all the conditions
        it specifies, plus the implicit conditions that it does not specify.
        """
        configurations = cls.get_permutations_of_configurations_in_file(filename)
        all_cond_names = set()
        for config in configurations:
            all_cond_names = all_cond_names.union(config.all_directives)

        result = set()
        for configuration in configurations:
            cond_names_in_configuration = configuration.all_directives
            cond_names_not_in_configuration = all_cond_names - cond_names_in_configuration
            for missing_cond_name in cond_names_not_in_configuration:
                configuration = configuration + (Condition(missing_cond_name, False), )
            result.add(cls(sorted(configuration)))

        # XXX: Previously, this produced eight configurations for gevent/corecext.ppyx
        # (containing all the possible permutations).
        # But two of them produced identical results and were hashed as such
        # by run_cython_on_files. We're now producing just the 6 results that
        # are distinct in that case. I'm not exactly sure why
        assert all(isinstance(x, Configuration) for x in result)
        return sorted(result)

class Condition(tuple):
    """
    A single CPP directive.

    Two-tuple: (name, True|False)
    """
    # Conditional directive:
    condition_re = re.compile(r'^#(ifdef\s+.+|if\s+.+|else\s*|endif\s*)$')

    _cache = {}

    __slots__ = ()

    def __new__(cls, *args):
        if len(args) == 2:
            # name, value; from literal constructor
            sequence = args
        elif len(args) == 1:
            sequence = args[0]
        else:
            raise TypeError("wrong argument number", args)

        if sequence not in cls._cache:
            if len(sequence) != 2:
                raise TypeError("Must be len 2", sequence)
            if not isinstance(sequence[0], str) or not isinstance(sequence[1], bool):
                raise TypeError("Must be (str, bool)")
            cls._cache[sequence] = tuple.__new__(cls, sequence)
        return cls._cache[sequence]

    def __repr__(self):
        return "Condition" + tuple.__repr__(self)

    @property
    def directive(self):
        return self[0]

    @property
    def value(self):
        return self[1]

    def format_cond(self):
        if self.value:
            return self.directive

        return '!' + self.directive

    def inverted(self):
        return Condition(self.directive, not self.value)

    @classmethod
    def match_condition(cls, line):
        line = line.strip()
        if line.endswith(':'):
            return None
        return cls.condition_re.match(line)

class ConfigurationGroups(tuple):
    """
    A sequence of Configurations that apply to the given line.

    These are maintained in sorted order.
    """

    _cache = {}

    def __new__(cls, tags):
        sorted_tags = tuple(sorted(tags))
        if sorted_tags not in cls._cache:
            if not all(isinstance(x, Configuration) for x in tags):
                raise TypeError("Must be a Configuration", tags)

            self = tuple.__new__(cls, sorted(tags))
            self._simplified = False
            cls._cache[sorted_tags] = self
        return cls._cache[sorted_tags]

    def __repr__(self):
        return "ConfigurationGroups" + tuple.__repr__(self)

    def __add__(self, other):
        l = list(self)
        l.extend(other)
        return ConfigurationGroups(l)

    def exact_reverse(self, tags2):
        if not self:
            return
        if not tags2:
            return
        if not isinstance(self, tuple):
            raise TypeError(repr(self))
        if not isinstance(tags2, tuple):
            raise TypeError(repr(tags2))
        if len(self) == 1 and len(tags2) == 1:
            tag1 = self[0]
            tag2 = tags2[0]
            assert isinstance(tag1, Configuration), tag1
            assert isinstance(tag2, Configuration), tag2
            if len(tag1) == 1 and len(tag2) == 1:
                tag1 = list(tag1)[0]
                tag2 = list(tag2)[0]
                if tag1[0] == tag2[0]:
                    return sorted([tag1[1], tag2[1]]) == [False, True]

    def format_tags(self):
        return ' || '.join('(%s)' % x.format_tag() for x in sorted(self))


    def simplify_tags(self):
        """
        >>> simplify_tags([set([('defined(world)', True), ('defined(hello)', True)]),
        ...                set([('defined(world)', False), ('defined(hello)', True)])])
        [set([('defined(hello)', True)])]
        >>> simplify_tags([set([('defined(LIBEV_EMBED)', True), ('defined(_WIN32)', True)]), set([('defined(LIBEV_EMBED)', True),
        ... ('defined(_WIN32)', False)]), set([('defined(_WIN32)', False), ('defined(LIBEV_EMBED)', False)]),
        ... set([('defined(LIBEV_EMBED)', False), ('defined(_WIN32)', True)])])
        []
        """
        if self._simplified:
            return self

        for tag1, tag2 in combinations(self, 2):
            if tag1 == tag2:
                tags = list(self)
                tags.remove(tag1)
                return ConfigurationGroups(tags).simplify_tags()

            for condition in tag1:
                inverted_condition = condition.inverted()
                if inverted_condition in tag2:
                    tag1_copy = tag1 - {inverted_condition}
                    tag2_copy = tag2 - {inverted_condition}

                    assert isinstance(tag1_copy, Configuration), tag1_copy
                    assert isinstance(tag2_copy, Configuration), tag2_copy

                    if tag1_copy == tag2_copy:
                        tags = list(self)
                        tags.remove(tag1)
                        tags.remove(tag2)
                        tags.append(tag1_copy)
                        return ConfigurationGroups(tags).simplify_tags()

        self._simplified = True
        return self


newline_token = ' <cythonpp.py: REPLACE WITH NEWLINE!> '

def _run_cython_on_file(configuration, pyx_filename,
                        py_banner, banner,
                        output_filename,
                        counter, lines,
                        cache=None):
    value = ''.join(lines)
    sourcehash = md5(value.encode("utf-8")).hexdigest()
    comment = configuration.format_tag() + " hash:" + str(sourcehash)
    if os.path.isabs(output_filename):
        raise ValueError("output cannot be absolute")
    # We can't change the actual name of the pyx file because
    # cython generates function names based in that string.
    # XXX: Note that this causes cython to generate
    # a "corecext" name instead of "gevent.corecext"
    tempdir = tempfile.mkdtemp()
    #unique_pyx_filename = pyx_filename
    #unique_output_filename = output_filename
    unique_pyx_filename = os.path.join(tempdir, pyx_filename)
    unique_output_filename = os.path.join(tempdir, output_filename)

    dirname = os.path.dirname(unique_pyx_filename) # output must be in same dir
    log("Output filename %s", unique_output_filename)
    if dirname and not os.path.exists(dirname):
        log("Making dir %s", dirname)
        os.makedirs(dirname)
    try:
        atomic_write(unique_pyx_filename, py_banner + value)
        if WRITE_OUTPUT:
            atomic_write(unique_pyx_filename + '.deb', '# %s (%s)\n%s' % (banner, comment, value))
        output = run_cython(unique_pyx_filename, sourcehash, unique_output_filename, banner, comment,
                            cache)
        if WRITE_OUTPUT:
            atomic_write(unique_output_filename + '.deb', output)
    finally:
        shutil.rmtree(tempdir, True)

    return configuration.attach_tags(output), configuration, sourcehash


def _run_cython_on_files(pyx_filename, py_banner, banner, output_filename, preprocessed):
    counter = 0
    threads = []
    cache = {}
    for configuration, lines in sorted(preprocessed.items()):
        counter += 1
        threads.append(Thread(target=_run_cython_on_file,
                              args=(configuration, pyx_filename,
                                    py_banner, banner, output_filename,
                                    counter, lines,
                                    cache)))
        threads[-1].start()

    for t in threads:
        t.join()

    same_results = {} # {sourcehash: tagged_str}
    for t in threads:
        sourcehash = t.value[2]
        tagged_output = t.value[0]
        if sourcehash not in same_results:
            same_results[sourcehash] = tagged_output
        else:
            # Nice, something to combine with tags
            other_tagged_output = same_results[sourcehash]
            assert len(tagged_output) == len(other_tagged_output)
            combined_lines = []
            for line_a, line_b in zip(tagged_output, other_tagged_output):
                combined_tags = line_a.tags + line_b.tags
                combined_lines.append(Str(line_a, combined_tags.simplify_tags()))
            same_results[sourcehash] = combined_lines

    # Order them as they were processed for repeatability
    ordered_results = []
    for t in threads:
        if t.value[0] not in ordered_results:
            ordered_results.append(same_results[t.value[2]])

    return ordered_results

def process_filename(filename, output_filename=None):
    """Process the .ppyx file with preprocessor and compile it with cython.

    The algorithm is as following:

        1) Identify all possible preprocessor conditions in *filename*.
        2) Run preprocess_filename(*filename*) for each of these conditions.
        3) Process the output of preprocessor with Cython (as many times as
           there are different sources generated for different preprocessor
           definitions.
        4) Merge the output of different Cython runs using preprocessor conditions
           identified in (1).
    """
    if output_filename is None:
        output_filename = filename.rsplit('.', 1)[0] + '.c'

    pyx_filename = filename.rsplit('.', 1)[0] + '.pyx'
    assert pyx_filename != filename

    timestamp = str(datetime.datetime.now().replace(microsecond=0))
    banner = 'Generated by cythonpp.py on %s' % timestamp
    py_banner = '# %s\n' % banner

    preprocessed = {}
    for configuration in Configuration.get_complete_configurations(filename):
        dbg("Processing %s", configuration)
        preprocessed[configuration] = preprocess_filename(filename, configuration)
    preprocessed[None] = preprocess_filename(filename, None)

    preprocessed = expand_to_match(preprocessed.items())
    reference_pyx = preprocessed.pop(None)

    sources = _run_cython_on_files(pyx_filename, py_banner, banner, output_filename,
                                   preprocessed)

    log('Generating %s ',  output_filename)
    result = generate_merged(sources)
    result_hash = md5(''.join(result.split('\n')[4:]).encode("utf-8")).hexdigest()
    atomic_write(output_filename, result)
    log('%s bytes of hash %s\n', len(result), result_hash)

    if filename != pyx_filename:
        log('Saving %s', pyx_filename)
        atomic_write(pyx_filename, py_banner + ''.join(reference_pyx))


def generate_merged(sources):
    result = []
    for line in produce_preprocessor(merge(sources)):
        result.append(line.replace(newline_token, '\n'))
    return ''.join(result)


def preprocess_filename(filename, config):
    """Process given .ppyx file with preprocessor.

    This does the following
        1) Resolves "#if"s and "#ifdef"s using config
        2) Expands macro definitions (#define)
    """
    linecount = 0
    current_name = None
    definitions = {}
    result = []
    including_section = []
    for line in open(filename):
        linecount += 1
        rstripped = line.rstrip()
        stripped = rstripped.lstrip()
        try:
            if current_name is not None:
                name = current_name
                value = rstripped
                if value.endswith('\\'):
                    value = value[:-1].rstrip()
                else:
                    current_name = None
                definitions[name]['lines'].append(value)
            else:
                if not including_section or including_section[-1]:
                    m = define_re.match(stripped)
                else:
                    m = None
                if m is not None:
                    name, params, value = m.groups()
                    value = value.strip()
                    if value.endswith('\\'):
                        value = value[:-1].rstrip()
                        current_name = name
                    definitions[name] = {'lines': [value]}
                    if params is None:
                        dbg('Adding definition for %r', name)
                    else:
                        definitions[name]['params'] = parse_parameter_names(params)
                        dbg('Adding definition for %r: %s', name, definitions[name]['params'])
                else:
                    m = Condition.match_condition(stripped)
                    if m is not None and config is not None:
                        if stripped == '#else':
                            if not including_section:
                                raise SyntaxError('unexpected "#else"')
                            if including_section[-1]:
                                including_section.pop()
                                including_section.append(False)
                            else:
                                including_section.pop()
                                including_section.append(True)
                        elif stripped == '#endif':
                            if not including_section:
                                raise SyntaxError('unexpected "#endif"')
                            including_section.pop()
                        else:
                            including_section.append(config.is_condition_true(stripped))
                    else:
                        if including_section and not including_section[-1]:
                            pass  # skip this line because last "#if" was false
                        else:
                            if stripped.startswith('#'):
                                # leave comments as is
                                result.append(Str_sourceline(line, linecount - 1))
                            else:
                                lines = expand_definitions(line, definitions).split('\n')
                                if lines and not lines[-1]:
                                    del lines[-1]
                                lines = [x + '\n' for x in lines]
                                lines = [Str_sourceline(x, linecount - 1) for x in lines]
                                result.extend(lines)
        except BaseException as ex:
            log('%s:%s: %s', filename, linecount, ex)
            if isinstance(ex, SyntaxError):
                sys.exit(1)
            else:
                raise
    return result


def merge(sources):
    r"""Merge different sources into a single one. Each line of the result
    is a subclass of string that maintains the information for each configuration
    it should appear in the result.

    >>> src1 = attach_tags('hello\nworld\n', set([('defined(hello)', True), ('defined(world)', True)]))
    >>> src2 = attach_tags('goodbye\nworld\n', set([('defined(hello)', False), ('defined(world)', True)]))
    >>> src3 = attach_tags('hello\neveryone\n', set([('defined(hello)', True), ('defined(world)', False)]))
    >>> src4 = attach_tags('goodbye\neveryone\n', set([('defined(hello)', False), ('defined(world)', False)]))
    >>> from pprint import pprint
    >>> pprint(merge([src1, src2, src3, src4]))
    [Str('hello\n', [set([('defined(hello)', True)])]),
     Str('goodbye\n', [set([('defined(hello)', False)])]),
     Str('world\n', [set([('defined(world)', True)])]),
     Str('everyone\n', [set([('defined(world)', False)])])]
    """
    sources = list(sources) # own copy
    dbg("Merging %s", len(sources))
    if len(sources) <= 1:
        return [Str(str(x), x.tags.simplify_tags()) for x in sources[0]]

    if not DEBUG:
        pool = multiprocessing.Pool()
    else:
        class SerialPool(object):
            def imap(self, func, arg_list):
                return [func(*args) for args in arg_list]

            def apply(self, func, args):
                return func(*args)
        pool = SerialPool()

    groups = []

    while len(sources) >= 2:
        one, two = sources.pop(), sources.pop()
        groups.append((one, two))

    dbg("Merge groups %s", len(groups))
    # len sources == 0 or 1
    for merged in pool.imap(_merge, groups):
        dbg("Completed a merge in %s", os.getpid())
        sources.append(merged)
        # len sources == 1 or 2

        if len(sources) == 2:
            one, two = sources.pop(), sources.pop()
            sources.append(pool.apply(_merge, (one, two)))
            # len sources == 1

    # len sources should now be 1
    dbg("Now merging %s", len(sources))
    return merge(sources)


def _merge(*args):
    if isinstance(args[0], tuple):
        a, b = args[0]
    else:
        a, b = args
    return list(_imerge(a, b))

def _imerge(a, b):
    # caching the tags speeds up serialization and future merges
    tag_cache = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == 'equal':
            for line_a, line_b in zip(a[i1:i2], b[j1:j2]):
                # tags is a tuple of frozensets
                line_a_tags = line_a.tags #getattr(line_a, 'tags', ())
                line_b_tags = line_b.tags #getattr(line_b, 'tags', ())
                key = (line_a_tags, line_b_tags)
                tags = tag_cache.setdefault(key, line_a_tags + line_b_tags)
                assert isinstance(tags, ConfigurationGroups)
                yield Str(line_a, tags)
        else:
            for line in a[i1:i2]:
                yield line
            for line in b[j1:j2]:
                yield line


def expand_to_match(items):
    """Insert empty lines so that all sources has matching line numbers for the same code"""
    cfg2newlines = {}  # maps configuration -> list
    for configuration, lines in items:
        cfg2newlines[configuration] = []

    maxguard = 2 ** 30
    while True:
        minimalsourceline = maxguard
        for configuration, lines in items:
            if lines:
                minimalsourceline = min(minimalsourceline, lines[0].sourceline)
        if minimalsourceline == maxguard:
            break

        for configuration, lines in items:
            if lines and lines[0].sourceline <= minimalsourceline:
                cfg2newlines[configuration].append(lines[0])
                del lines[0]

        number_of_lines = max(len(x) for x in cfg2newlines.values())

        for newlines in cfg2newlines.values():
            add = (number_of_lines - len(newlines))
            newlines.extend(['\n'] * add)

    return cfg2newlines


def produce_preprocessor(iterable):

    if DEBUG:
        current_line = [0]

        def wrap(line):
            current_line[0] += 1
            dbg('%5d: %s', current_line[0], repr(str(line))[1:-1])
            return line
    else:
        def wrap(line):
            return line

    state = None
    for line in iterable:
        key = line.tags# or None

        if key == state:
            yield wrap(line)
        else:
            if key.exact_reverse(state):
                yield wrap('#else /* %s */\n' % state.format_tags())
            else:
                if state:
                    yield wrap('#endif /* %s */\n' % state.format_tags())
                if key:
                    yield wrap('#if %s\n' % key.format_tags())
            yield wrap(line)
            state = key
    if state:
        yield wrap('#endif /* %s */\n' % state.format_tags())

class Str(str):
    """This is a string subclass that has a set of tags attached to it.

    Used for merging the outputs.
    """

    def __new__(cls, string, tags):
        if not isinstance(string, str):
            raise TypeError('string must be str: %s' % (type(string), ))
        if not isinstance(tags, Configuration) and not isinstance(tags, ConfigurationGroups):
            raise TypeError("Must be tags or tag groups: %r" % (tags,))
        if isinstance(tags, Configuration):
            tags = ConfigurationGroups((tags,))

        self = str.__new__(cls, string)
        self.tags = tags
        return self

    def __getnewargs__(self):
        return str(self), self.tags

    def __repr__(self):
        return '%s(%s, %r)' % (self.__class__.__name__, str.__repr__(self), self.tags)

    def __add__(self, other):
        if not isinstance(other, str):
            raise TypeError
        return self.__class__(str.__add__(self, other), self.tags)

    def __radd__(self, other):
        if not isinstance(other, str):
            raise TypeError
        return self.__class__(str.__add__(other, self), self.tags)

    methods = ['__getslice__', '__getitem__', '__mul__', '__rmod__', '__rmul__',
               'join', 'replace', 'upper', 'lower']

    for method in methods:
        do_exec('''def %s(self, *args):
    return self.__class__(str.%s(self, *args), self.tags)''' % (method, method), locals())




def parse_parameter_names(x):
    assert x.startswith('(') and x.endswith(')'), repr(x)
    x = x[1:-1]
    result = []
    for param in x.split(','):
        param = param.strip()
        if not param_name_re.match(param):
            raise SyntaxError('Invalid parameter name: %r' % param)
        result.append(param)
    return result


def parse_parameter_values(x):
    assert x.startswith('(') and x.endswith(')'), repr(x)
    x = x[1:-1]
    result = []
    for param in x.split(','):
        result.append(param.strip())
    return result


def expand_definitions(code, definitions):
    if not definitions:
        return code
    keys = list(definitions.keys())
    keys.sort(key=lambda x: (-len(x), x))
    keys = '|'.join(keys)

    # This regex defines a macro invocation
    re_macro = re.compile(r'(^|##|[^\w])(%s)(\([^)]+\)|$|##|[^w])' % keys)

    def repl(m):
        token = m.group(2)
        definition = definitions[token]

        params = definition.get('params', [])

        if params:
            arguments = m.group(3)
            if arguments.startswith('(') and arguments.endswith(')'):
                arguments = parse_parameter_values(arguments)
            else:
                arguments = None
            if arguments and len(params) == len(arguments):
                local_definitions = {}
                dbg('Macro %r params=%r arguments=%r source=%r', token, params, arguments, m.groups())
                for key, value in zip(params, arguments):
                    dbg('Adding argument %r=%r', key, value)
                    local_definitions[key] = {'lines': [value]}
                result = expand_definitions('\n'.join(definition['lines']), local_definitions)
            else:
                msg = 'Invalid number of arguments for macro %s: expected %s, got %s'
                msg = msg % (token, len(params), len(arguments or []))
                raise SyntaxError(msg)
        else:
            result = '\n'.join(definition['lines'])
            if m.group(3) != '##':
                result += m.group(3)
        if m.group(1) != '##':
            result = m.group(1) + result
        dbg('Replace %r with %r', m.group(0), result)
        return result

    for _ in range(20000):
        newcode, count = re_macro.subn(repl, code, count=1)
        if code == newcode:
            if count > 0:
                raise SyntaxError('Infinite recursion')
            return newcode
        code = newcode
    raise SyntaxError('Too many substitutions or internal error.')


class Str_sourceline(str):

    def __new__(cls, source, sourceline):
        self = str.__new__(cls, source)
        self.sourceline = sourceline
        return self

    def __getnewargs__(self):
        return str(self), self.sourceline

def atomic_write(filename, data):
    tmpname = filename + '.tmp.%s' % os.getpid()
    with open(tmpname, 'w') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    if os.path.exists(filename):
        os.unlink(filename)
    os.rename(tmpname, filename)
    dbg('Wrote %s bytes to %s', len(data), filename)


def run_cython(filename, sourcehash, output_filename, banner, comment, cache=None):
    dbg("Cython output to %s hash %s", output_filename, sourcehash)
    result = cache.get(sourcehash) if cache is not None else None
    # Use an array for the argument so that filename arguments are properly
    # quoted according to local convention
    command = [CYTHON, '-o', output_filename, '-I', 'gevent', filename]
    if result is not None:
        log('Reusing %s  # %s', command, comment)
        return result
    system(command, comment)
    result = postprocess_cython_output(output_filename, banner)
    if cache is not None:
        cache[sourcehash] = result
    return result


def system(command, comment):
    command_str = ' '.join(command)
    log('Running %s  # %s', command_str, comment)
    try:
        subprocess.check_call(command)
        dbg('\tDone running %s # %s', command_str, comment)
    except subprocess.CalledProcessError:
        # debugging code
        log("Path: %s", os.getenv("PATH"))
        bin_dir = os.path.dirname(sys.executable)
        bin_files = os.listdir(bin_dir)
        bin_files.sort()
        log("Bin: %s files: %s", bin_dir, ' '.join(bin_files))
        raise


def postprocess_cython_output(filename, banner):
    # this does a few things:
    # 1) converts multiline C-style (/**/) comments with a single line comment by
    #    replacing \n with newline_token
    # 2) adds our header
    # 3) remove timestamp in cython's header so that different timestamps do not
    #    confuse merger
    result = ['/* %s */\n' % (banner)]

    with open(filename) as finput:
        firstline = finput.readline()

        m = cython_header_re.match(firstline.strip())
        if m:
            result.append('/* %s */' % m.group(1))
        else:
            result.append(firstline)

        in_comment = False
        for line in finput:

            if line.endswith('\n'):
                line = line[:-1].rstrip() + '\n'

            if in_comment:
                if '*/' in line:
                    in_comment = False
                    result.append(line)
                else:
                    result.append(line.replace('\n', newline_token))
            else:
                if line.lstrip().startswith('/* ') and '*/' not in line:
                    line = line.lstrip()  # cython adds space before /* for some reason
                    line = line.replace('\n', newline_token)
                    result.append(line)
                    in_comment = True
                else:
                    result.append(line)
    return ''.join(result)

def log(message, *args):
    try:
        string = message % args
    except Exception:
        try:
            prefix = 'Traceback (most recent call last):\n'
            lines = traceback.format_stack()[:-1]
            error_lines = traceback.format_exc().replace(prefix, '')
            last_length = len(lines[-1].strip().rsplit('    ', 1)[-1])
            last_length = min(80, last_length)
            last_length = max(5, last_length)
            msg = '%s%s    %s\n%s' % (prefix, ''.join(lines), '^' * last_length, error_lines)
            sys.stderr.write(msg)
        except Exception:
            traceback.print_exc()
        try:
            message = '%r %% %r\n\n' % (message, args)
        except Exception:
            pass
        try:
            sys.stderr.write(message)
        except Exception:
            traceback.print_exc()
    else:
        print(string, file=sys.stderr)


def dbg(*args):
    if not DEBUG:
        return
    return log(*args)


def main():
    import optparse
    parser = optparse.OptionParser()
    parser.add_option('--debug', action='store_true')
    parser.add_option('--list', action='store_true', help='Show the list of different conditions')
    parser.add_option('--list-cond', action='store_true')
    parser.add_option('--ignore-cond', action='store_true', help='Ignore conditional directives (only expand definitions)')
    parser.add_option('--write-intermediate', action='store_true', help='Save intermediate files produced by preprocessor and Cython')
    parser.add_option('-o', '--output-file', help='Specify name of generated C file')

    options, args = parser.parse_args()
    if len(args) != 1:
        sys.exit('Expected one argument (filename), got %r' % args)
    filename = args[0]

    if options.debug:
        DEBUG = True

    if options.write_intermediate:
        WRITE_OUTPUT = True

    run = True

    if options.list_cond:
        run = False
        for x in get_conditions(filename):
            sys.stdout.write('* %s\n' % (x, ))

    if options.list:
        run = False
        for x in get_configurations(filename):
            sys.stdout.write('* %s\n' % (x, ))

    if options.ignore_cond:
        run = False

        class FakeConfig(object):
            def is_condition_true(*args):
                return False

        sys.stdout.write(preprocess_filename(filename, FakeConfig()))

    if run:
        process_filename(filename, options.output_file)


if __name__ == '__main__':
    main()
