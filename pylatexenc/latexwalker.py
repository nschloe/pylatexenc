# -*- coding: utf-8 -*-
#
# The MIT License (MIT)
# 
# Copyright (c) 2018 Philippe Faist
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

r"""
The ``latexwalker`` module provides a simple API for parsing LaTeX snippets,
and representing the contents using a data structure based on node classes.

LatexWalker will understand the syntax of most common macros.  However,
``latexwalker`` is NOT a replacement for a full LaTeX engine.  (Originally,
``latexwalker`` was desigend to extract useful text for indexing for text
database searches of LaTeX content.)

You can also use `latexwalker` directly in command-line, producing JSON or a
human-readable node tree::

    $ echo '\textit{italic} text' | latexwalker  \ 
                                    --output-format=json --json-compact
    {"nodelist": [{"nodetype": "LatexMacroNode", "pos": 0, "len": 15, [...]

    $ latexwalker --help
    [...]

This module provides the main machinery to parse a chunk of LaTeX code.  The
parser can be influenced by specifying a collection of known macros and
environments (the "latex context") that are specified using
:py:class:`macrospec.MacroSpec` and :py:class:`macrospec.EnvironmentSpec`
objects in a :py:class:`macrospec.LatexContextDb` object.  See the doc of the
module :py:mod:`macrospec` for more information.
"""

from __future__ import print_function

import re
from collections import namedtuple
import sys
import logging
import json

from . import macrospec
from . import _util

if sys.version_info.major > 2:
    # Py3
    def unicode(string): return string
    _basestring = str
    _str_from_unicode = lambda x: x
    _unicode_from_str = lambda x: x
else:
    # Py2
    _basestring = basestring
    _str_from_unicode = lambda x: unicode(x).encode('utf-8')
    _unicode_from_str = lambda x: x.decode('utf-8')

logger = logging.getLogger(__name__)



class LatexWalkerError(Exception):
    """
    Generic exception class raised by this module.
    """
    pass

class LatexWalkerParseError(LatexWalkerError):
    """
    Parse error.  The following attributes are available: `msg` (the error message), `s`
    (the parsed string), `pos` (the position of the error in the string, 0-based index).
    """
    def __init__(self, msg, s=None, pos=None):
        self.msg = msg
        self.s = s
        self.pos = pos
        disp = '...'+s[max(pos-25,0):pos]
        disp = '\n%s\n'%(disp)  +  (' '*len(disp)) + s[pos:pos+25]+'...'
        super(LatexWalkerParseError, self).__init__(
            msg + ( " @ %d:\n%s" %(pos, disp) )
        )

class LatexWalkerEndOfStream(LatexWalkerError):
    """
    Reached end of input stream (e.g., end of file).
    """
    pass






def get_default_latex_context_db():
    r"""
    Return a :py:class:`pylatexenc.macrospec.LatexContextDb` instance
    initialized with a collection of known macros and environments.

    TODO: document categories.

    If you want to add your own definitions, you should use the
    :py:meth:`pylatexenc.macrospec.LatexContextDb.add_context_category()`
    method.  If you would like to override some definitions, use that method
    with the argument `prepend=True`.  See docs for
    :py:meth:`pylatexenc.macrospec.LatexContextDb.add_context_category()`.

    If there are too many macro/environment definitions, or if there are some
    irrelevant ones, you can always filter the returned database using
    :py:meth:`pylatexenc.macrospec.LatexContextDb.filter_context()`.

    .. versionadded:: 2.0
 
       The :py:class:`pylatexenc.macrospec.LatexContextDb` class as well as this
       method, were all introduced in `pylatexenc 2.0`.
    """
    db = macrospec.LatexContextDb()
    
    from ._latexwalker_defaultspecs import specs

    for cat, catspecs in specs:
        db.add_context_category(cat,
                                macros=catspecs['macros'],
                                environments=catspecs['environments'],
                                specials=catspecs['specials'])
    
    return db
    





# provide an interface compatibile with pylatexenc < 2
MacrosDef = macrospec.std_macro
r"""
.. deprecated:: 2.0

   Use :py:func:`pylatexenc.macrospec.std_macro` instead which does the same
   thing, or invoke the :py:class:`~pylatexenc.macrospec.MacroSpec` class
   directly (or a subclass).

   Since `pylatexenc 2.0`, `MacrosDef` is an alias to the function
   :py:func:`pylatexenc.macrospec.std_macro` which returns a
   :py:class:`~pylatexenc.macrospec.MacroSpec` instance.  In this way the
   earlier idiom ``MacrosDef(...)`` still works in `pylatexenc 2`.
"""

default_macro_dict = _util.LazyDict(
    generate_dict_fn=lambda: dict([
        (m.macroname, m)
        for m in get_default_latex_context_db().iter_macro_specs()
    ])
)
r"""
.. deprecated:: 2.0

   Use :py:func:`get_default_latex_context_db()` instead, or create your own
   :py:class:`pylatexenc.macrospec.LatexContextDb` object.


Provide an access to the default macro specs for `latexwalker` in a form
that is compatible with `pylatexenc 1.x`\ 's `default_macro_dict` module-level
dictionary.

This is implemented using a custom lazy mutable mapping, which behaves just like
a regular dictionary but that loads the data only once the dictionary is
accessed.  In this way the default latex specs into a python dictionary unless
they are actually queried or modified, and thus users of `pylatexenc 2.0` that
don't rely on the default macro/environment definitions shouldn't notice any
decrease in performance.
"""



# ------------------------------------------------


class LatexToken(object):
    r"""
    Represents a token read from the LaTeX input.

    This is used internally by :py:class:`LatexWalker`'s methods.  You probably
    don't need to worry about individual tokens.  Rather, you should use the
    high-level functions provided by :py:class:`LatexWalker` (e.g.,
    :py:meth:`~latexwalker.LatexWalker.get_latex_nodes()`).  So most likely, you
    can ignore this class entirely.

    Instances of this class are what the method
    :py:meth:`LatexWalker.get_token()` returns.  See the doc of that function
    for more information on how tokens are parsed.

    This is not the same thing as a LaTeX token, it's just a part of the input
    which we treat in the same way (e.g. a bunch of content characters, a
    comment, a macro, etc.)

    Information about the object is stored into the fields `tok` and `arg`. The
    `tok` field is a string which identifies the type of the token. The `arg`
    depends on what `tok` is, and describes the actual input.

    Additionally, this class stores information about the position of the token
    in the input stream in the field `pos`.  This `pos` is an integer which
    corresponds to the index in the input string.  The field `len` stores the
    length of the token in the input string.  This means that this token spans
    in the input string from `pos` to `pos+len`.

    Leading whitespace before the token is not returned as a separate
    'char'-type token, but it is given in the `pre_space` field of the token
    which follows.  Pre-space may contain a newline, but not two consecutive
    newlines.

    The `post_space` is only used for 'macro' and 'comment' tokens, and it
    stores any spaces encountered after a macro, or the newline with any
    following spaces that terminates a LaTeX comment.

    The `tok` field may be one of:

      - 'char': raw character(s) which have no special LaTeX meaning and which
        are part of the text content.
        
        The `arg` field contains the characters themselves.

      - 'macro': a macro invokation, but not '\begin' or '\end'
        
        The `arg` field contains the name of the macro, without the leading
        backslash.

      - 'begin_environment': an invokation of '\begin{environment}'.
        
        The `arg` field contains the name of the environment inside the braces.

      - 'end_environment': an invokation of '\end{environment}'.
        
        The `arg` field contains the name of the environment inside the braces.

      - 'comment': a LaTeX comment delimited by a percent sign up to the end of
        the line.
        
        The `arg` field contains the text in the comment line, not including the
        percent sign nor the newline.

      - 'brace_open': an opening brace.  This is usually a curly brace, and
        sometimes also a square bracket.  What is parsed as a brace depends on
        the arguments to :py:func:`get_token()`.
        
        The `arg` is a string which contains the relevant brace character.
        
      - 'brace_close': a closing brace.  This is usually a curly brace, and
        sometimes also a square bracket.  What is parsed as a brace depends on
        the arguments to :py:func:`get_token()`.
        
        The `arg` is a string which contains the relevant brace character.

      - 'mathmode_inline': a delimiter which starts/ends inline math.  This is
        (e.g.) a single '$' character which is not part of a double '$$'
        display environment delimiter.

        The `arg` is the string value of the delimiter in question ('$')

      - 'mathmode_display': a delimiter which starts/ends display math, e.g.,
        ``\[``.

        The `arg` is the string value of the delimiter in question (e.g.,
        ``\[`` or ``$$``)

      - 'specials': a character or character sequence that has a special
        meaning in LaTeX.  E.g., '~', '&', etc.

        The `arg` field is then the corresponding
        :py:class:`~pylatexenc.macrospec.SpecialsSpec` instance.  [The rationale
        for setting `arg` to a `SpecialsSpec` instance, in contrast to the
        behavior for macros and envrionments, is that macros and environments
        are delimited directly by LaTeX syntax and are determined unambiguously
        without any lookup in the latex context database.  This is not the case
        for specials.]
    """
    def __init__(self, tok, arg, pos, len, pre_space, post_space=''):
        self.tok = tok
        self.arg = arg
        self.pos = pos
        self.len = len
        self.pre_space = pre_space
        self.post_space = post_space
        self._fields = ['tok', 'arg', 'pos', 'len', 'pre_space']
        if self.tok in ('macro', 'comment'):
            self._fields.append('post_space')
        super(LatexToken, self).__init__()


    def __unicode__(self):
        return _unicode_from_str(self.__str__())

    def __repr__(self):
        return (
            "LatexToken(" +
            ", ".join([ "%s=%r"%(k,getattr(self,k))
                        for k in self._fields ]) +
            ")"
            )

    def __str__(self):
        return self.__repr__()

    def __eq__(self, other):
        return all( ( getattr(self, f) == getattr(other, f)  for f in self._fields ) )

    # see https://docs.python.org/3/library/constants.html#NotImplemented
    def __ne__(self, other): return NotImplemented

    __hash__ = None


# ------------------------------------------------



class ParsedContext(object):
    r"""
    Stores some essential information that is associated with
    :py:class:`LatexNode`\ 's and which provides a context to better understand
    the node structure.  For instance, we store the original parsed string, and
    each node refers to which part of the string they represent.
    
    .. py:attribute:: s

       The string that is parsed by the :py:class:`LatexWalker`

    .. py:attribute:: latex_context

       The latex context (with macros/environments specifications) that was used
       when parsing the string `s`.

    """

    def __init__(self, s, latex_context):
        self.s = s
        self.latex_context = latex_context
        super(ParsedContext, self).__init__()




# ------------------------------------------------




class LatexNode(object):
    """
    Represents an abstract 'node' of the latex document.

    Use :py:meth:`nodeType()` to figure out what type of node this is, and
    :py:meth:`isNodeType()` to test whether it is of a given type.

    All nodes have the following attributes:

    .. py:attribute:: parsed_context

       The context object that stores additional context information for this
       node.

    .. py:attribute:: pos

       The position in the parsed string that this node represents.  The parsed
       string can be recovered as `parsed_context.s`, see
       :py:attr:`ParsedContext.s`.

    .. py:attribute:: len

       How many characters in the parsed string this node represents, starting
       at position `pos`.  The parsed string can be recovered as
       `parsed_context.s`, see :py:attr:`ParsedContext.s`.

    """
    def __init__(self, _fields, _redundant_fields=None,
                 parsed_context=None, pos=None, len=None, **kwargs):
        """
        Important: subclasses must specify a list of fields they set in the
        `_fields` argument.  They should only specify base (non-redundant)
        fields; if they have "redundant" fields, specify the additional fields
        in _redundant_fields=...
        """
        super(LatexNode, self).__init__(**kwargs)
        self.parsed_context = parsed_context
        self.pos = pos
        self.len = len
        self._fields = tuple(['pos', 'len'] + list(_fields))
        self._redundant_fields = self._fields
        if _redundant_fields is not None:
            self._redundant_fields = tuple(list(self._fields) + list(_redundant_fields))

    def nodeType(self):
        """
        Returns the class which corresponds to the type of this node.  This is a
        Python class object, that is one of
        :py:class:`~pylatexenc.latexwalker.LatexCharsNode`,
        :py:class:`~pylatexenc.latexwalker.LatexGroupNode`, etc.
        """
        return LatexNode

    def isNodeType(self, t):
        """
        Returns `True` if the current node is of the given type.  The argument `t`
        must be a Python class such as,
        e.g. :py:class:`~pylatexenc.latexwalker.LatexGroupNode`.
        """
        return isinstance(self, t)

    def latex_verbatim(self):
        r"""
        Return the chunk of LaTeX code that this node represents.

        This is a shorthand for ``node.parsed_context.s[node.pos:node.pos+node.len]``.
        """
        return self.parsed_context.s[self.pos : self.pos+self.len]

    def __eq__(self, other):
        return other is not None  and  \
            self.nodeType() == other.nodeType()  and  \
            other.parsed_context is self.parsed_context and \
            other.pos == self.pos and \
            other.len == self.len and \
            all(
                ( getattr(self, f) == getattr(other, f)  for f in self._fields )
            )

    # see https://docs.python.org/3/library/constants.html#NotImplemented
    def __ne__(self, other): return NotImplemented

    __hash__ = None

    def __unicode__(self):
        return _unicode_from_str(self.__str__())
    def __str__(self):
        return self.__repr__()
    def __repr__(self):
        return (
            self.nodeType().__name__ + "(" +
            ", ".join([ "%s=%r"%(k,getattr(self,k))  for k in self._fields ]) +
            ")"
            )


class LatexCharsNode(LatexNode):
    """
    A string of characters in the LaTeX document, without any special LaTeX
    code.

    .. py:attribute:: chars

       The string of characters represented by this node.
    """
    def __init__(self, chars, **kwargs):
        super(LatexCharsNode, self).__init__(
            _fields = ('chars',),
            **kwargs
        )
        self.chars = chars

    def nodeType(self):
        return LatexCharsNode

class LatexGroupNode(LatexNode):
    r"""
    A LaTeX group delimited by braces, ``{like this}``.

    Note: in the case of an optional macro or environment argument, this node is
    also used to represents a group delimited by square braces instead of curly
    braces.

    .. py:attribute:: nodelist

       A list of nodes describing the contents of the LaTeX braced group.  Each
       item of the list is a :py:class:`LatexNode`.
    """
    def __init__(self, nodelist, **kwargs):
        super(LatexGroupNode, self).__init__(
            _fields=('nodelist',),
            **kwargs
        )
        self.nodelist = nodelist

    def nodeType(self):
        return LatexGroupNode

class LatexCommentNode(LatexNode):
    r"""
    A LaTeX comment, delimited by a percent sign until the end of line.

    .. py:attribute:: comment

       The comment string, not including the '%' sign nor the following newline

    .. py:attribute:: comment_post_space

       The newline that terminated the comment possibly followed by spaces
       (e.g., indentation spaces of the next line)

    """
    def __init__(self, comment, **kwargs):
        comment_post_space = kwargs.pop('comment_post_space', '')

        super(LatexCommentNode, self).__init__(
            _fields = ('comment', 'comment_post_space', ),
            **kwargs
        )

        self.comment = comment
        self.comment_post_space = comment_post_space

    def nodeType(self):
        return LatexCommentNode

class LatexMacroNode(LatexNode):
    r"""
    Represents a macro type node, e.g. ``\textbf``

    .. py:attribute:: macroname

       The name of the macro (string), *without* the leading backslash.

    .. py:attribute:: nodeargd

       The :py:class:`pylatexenc.macrospec.ParsedMacroArgs` object that
       represents the macro arguments.

       For macros that do not accept any argument, this is an empty
       :py:class:`~pylatexenc.macrospec.ParsedMacroArgs` instance.  The
       attribute `nodeargd` can be `None` even for macros that accept arguments,
       in the situation where :py:meth:`LatexWalker.get_latex_expression()`
       encounters the macro when reading a single expression.

    .. py:attribute:: macro_post_space

       Any spaces that were encountered immediately after the macro.

    The following attributes are obsolete since `pylatexenc 2.0`.

    .. py:attribute:: nodeoptarg

       .. deprecated:: 2.0

          Macro arguments are stored in `nodeargd` in `pylatexenc 2`.  Accessing
          the argument `nodeoptarg` will still give a first optional argument
          for standard latex macros, for backwards compatibility.

       If non-`None`, this corresponds to the optional argument of the macro.

    .. py:attribute:: nodeargs

       .. deprecated:: 2.0

          Macro arguments are stored in `nodeargd` in pylatexenc 2.  Accessing
          the argument `nodeargs` will still provide a list of argument nodes
          for standard latex macros, for backwards compatibility.

       A list of arguments to the macro. Each item in the list is a
       :py:class:`LatexNode`.
    """
    def __init__(self, macroname, **kwargs):
        nodeargd=kwargs.pop('nodeargd', macrospec.ParsedMacroArgs())
        macro_post_space=kwargs.pop('macro_post_space', '')
        # legacy:
        nodeoptarg=kwargs.pop('nodeoptarg', None)
        nodeargs=kwargs.pop('nodeargs', [])

        super(LatexMacroNode, self).__init__(
            _fields = ('macroname','nodeargd','macro_post_space'),
            _redundant_fields = ('nodeoptarg','nodeargs'),
            **kwargs)

        self.macroname = macroname
        self.nodeargd = nodeargd
        self.macro_post_space = macro_post_space
        # legacy:
        self.nodeoptarg = nodeoptarg
        self.nodeargs = nodeargs

    def nodeType(self):
        return LatexMacroNode



class LatexEnvironmentNode(LatexNode):
    r"""
    A LaTeX Environment Node, i.e. ``\begin{something} ... \end{something}``.

    .. py:attribute:: envname

       The name of the environment ('itemize', 'equation', ...)

    .. py:attribute:: nodelist

       A list of :py:class:`LatexNode`'s that represent all the contents between
       the ``\begin{...}`` instruction and the ``\end{...}`` instruction.

    .. py:attribute:: nodeargd

       The :py:class:`pylatexenc.macrospec.ParsedMacroArgs` object that
       represents the macro arguments.

    The following attributes are obsolete since `pylatexenc 2.0`.

    .. py:attribute:: optargs

       .. deprecated:: 2.0

          Macro arguments are stored in `nodeargd` in `pylatexenc 2`.  Accessing
          the argument `optargs` will still give a list of initial optional
          arguments for standard latex macros, for backwards compatibility.

    .. py:attribute:: args

       .. deprecated:: 2.0

          Macro arguments are stored in `nodeargd` in `pylatexenc 2`.  Accessing
          the argument `args` will still give a list of curly-brace-delimited
          arguments for standard latex macros, for backwards compatibility.
    """
    
    def __init__(self, envname, nodelist, **kwargs):
        nodeargd = kwargs.pop('nodeargd', macrospec.ParsedMacroArgs())
        # legacy:
        optargs = kwargs.pop('optargs', [])
        args = kwargs.pop('args', [])

        super(LatexEnvironmentNode, self).__init__(
            _fields = ('envname','nodelist','nodeargd',),
            _redundant_fields = ('optargs','args',),
            **kwargs)

        self.envname = envname
        self.nodelist = nodelist
        self.nodeargd = nodeargd
        # legacy:
        self.optargs = optargs
        self.args = args

    def nodeType(self):
        return LatexEnvironmentNode

class LatexSpecialsNode(LatexNode):
    r"""
    Represents a specials type node, e.g. ``&`` or ``~``

    .. py:attribute:: specials_chars

       The name of the specials (string), *without* the leading backslash.

    .. py:attribute:: nodeargd

       If the specials spec (cf. :py:class:`~pylatexenc.macrospec.SpecialsSpec`)
       has `args_parser=None` then the attribute `nodeargd` is set to `None`.
       If `args_parser` is specified in the spec, then the attribute `nodeargd`
       is a :py:class:`pylatexenc.macrospec.ParsedMacroArgs` instance that 
       represents the arguments to the specials.

       The `nodeargd` attribute can also be `None` even if the specials expects
       arguments, in the special situation where
       :py:meth:`LatexWalker.get_latex_expression()` encounters this specials.
    """
    def __init__(self, specials_chars, **kwargs):
        nodeargd=kwargs.pop('nodeargd', None)

        super(LatexSpecialsNode, self).__init__(
            _fields = ('specials_chars','nodeargd'),
            **kwargs)

        self.specials_chars = specials_chars
        self.nodeargd = nodeargd

    def nodeType(self):
        return LatexSpecialsNode




class LatexMathNode(LatexNode):
    r"""
    A Math node type.

    Note that currently only 'inline' math environments are detected.

    .. py:attribute:: displaytype

       Either 'inline' or 'display', to indicate an inline math block or a
       display math block. (Note that math environments such as
       `\begin{equation}...\end{equation}`, are reported as
       :py:class:`LatexEnvironmentNode`'s, and not as
       :py:class:`LatexMathNode`'s.

    .. py:attribute:: delimiters

       A 2-item tuple containing the begin and end delimiters used to delimit
       this math mode section.

    .. py:attribute:: nodelist
    
       The contents of the environment, given as a list of
       :py:class:`LatexNode`'s.
    """
    def __init__(self, displaytype, nodelist=[], **kwargs):
        delimiters = kwargs.pop('delimiters', (None, None))

        super(LatexMathNode, self).__init__(
            _fields = ('displaytype','nodelist','delimiters'),
            **kwargs
        )

        self.displaytype = displaytype
        self.nodelist = nodelist
        self.delimiters = delimiters

    def nodeType(self):
        return LatexMathNode


# ------------------------------------------------------------------------------


class _PushPropOverride(object):
    def __init__(self, obj, propname, new_value):
        super(_PushPropOverride, self).__init__()
        self.obj = obj
        self.propname = propname
        self.new_value = new_value

    def __enter__(self):
        if self.new_value is not None:
            self.initval = getattr(self.obj, self.propname)
            setattr(self.obj, self.propname, self.new_value)
        return self

    def __exit__(self, type, value, traceback):
        # clean-up
        if self.new_value is not None:
            setattr(self.obj, self.propname, self.initval)


class ParsingContext(object):
    r"""
    Stores some information about the current parsing context, such as whether
    we are currently in a math mode block.

    One of the ideas of `pylatexenc` is to make the parsing of LaTeX code mostly
    context-independent mark-up parsing.  However a minimal context might come
    in handy sometimes.  Perhaps some macros or specials should behave
    differently in math mode than in text mode.

    Currently, we only track whether or not we are in math mode.  There are no
    concrete plans to include much more context information in the future.
    Nevertheless the current API is designed so that further context properties
    can easily be added in the future.

    .. py:attribute:: in_math_mode

       Whether or not the chunk of LaTeX code that we are currently parsing is
       in math mode (True or False)

    .. versionadded:: 2.0
 
       This class was introduced in version 2.0.
    """
    def __init__(self, in_math_mode=False):
        super(ParsingContext, self).__init__()
        self.in_math_mode = in_math_mode
        self._fields = ('in_math_mode', )

    def sub_context(self, **kwargs):
        r"""
        Return a new :py:class:`ParsingContext` instance that is a copy of the
        current parsing context, but where the given properties keys have been
        set to the corresponding values (given as keyword arguments).

        This makes it easy to create a sub-context in a given parser.  For
        instance, if we enter math mode, we might write::

           parsing_context_inner = parsing_context.sub_context(in_math_mode=True)
        """
        p = ParsingContext(**dict([(f, getattr(self, f)) for f in self._fields]))
        for k, v in kwargs.items():
            if k not in self._fields:
                raise ValueError("Invalid field for ParsingContext: {}={!r}".format(k, v))
            setattr(p, k, v)
        return p


# ------------------------------------------------------------------------------

class LatexWalker(object):
    r"""
    A parser which walks through an input stream, parsing it as LaTeX markup.

    Arguments:

      - `s`: the string to parse as LaTeX code

      - `latex_context`: a :py:class:`pylatexenc.macrospec.LatexContextDb`
        object that provides macro and environment specifications with
        instructions on how to parse arguments, etc.  If you don't specify this
        argument, or if you specify `None`, then the default database is used.
        The default database is obtained with
        :py:func:`get_default_latex_context_db()`.

        .. versionadded:: 2.0

           This `latex_context` argument was introduced in version 2.0.

    Additional keyword arguments are flags which influence the parsing.
    Accepted flags are:

      - `tolerant_parsing=True|False` If set to `True`, then the parser
        generally ignores syntax errors rather than raising an exception.

      - `strict_braces=True|False` This option refers specifically to reading a
        encountering a closing brace when an expression is needed.  You
        generally won't need to specify this flag, use `tolerant_parsing`
        instead.

    The methods provided in this class perform various parsing of the given
    string `s`.  These methods typically accept a `pos` parameter, which must be
    an integer, which defines the position in the string `s` to start parsing.

    These methods, unless otherwise documented, return a tuple `(node, pos,
    len)`, where node is a :py:class:`LatexNode` describing the parsed content,
    `pos` is the position at which the LaTeX element of iterest was encountered,
    and `len` is the length of the string that is considered to be part of the
    `node`.  That is, the position in the string that is immediately after the
    node is `pos+len`.

    The following obsolete flag is accepted by the constructor for backwards
    compatibility with `pylatexenc < 2`:

      - `macro_dict`: a dictionary of known LaTeX macro specifications.  If
        specified, this should be a dictionary where the keys are macro names
        and values are :py:class:`pylatexenc.macrospec.MacroSpec` instances.  If
        you specify this argument, you cannot provide a custom `latex_context`.
        This argument is superseded by the `latex_context` argument.

        .. deprecated:: 2.0
    
           The `macro_dict` argument has been replaced by the much more powerful
           `latex_context` argument which allows you to further provide
           environment specifications, etc.

      - `keep_inline_math=True|False`: Obsolete option.  In `pylatexenc < 2`,
        this option triggered a weird behavior especially since there is a
        similarly named option in
        :py:class:`pylatexenc.latex2text.LatexNodes2Text` with a different
        meaning.  [See `Issue #14
        <https://github.com/phfaist/pylatexenc/issues/14>`_.]  You should now
        only use the option `math_mode=` in
        :py:class:`pylatexenc.latex2text.LatexNodes2Text`.

        .. deprecated:: 2.0

           This option is ignored starting from `pylatexenc 2`.  Instead, you
           should set the option `math_mode=` accordingly in
           :py:class:`pylatexenc.latex2text.LatexNodes2Text`.
    """

    def __init__(self, s, latex_context=None, **kwargs):

        self.s = s

        if latex_context is None:
            if 'macro_dict' in kwargs:
                # LEGACY -- build a latex context using the given macro_dict
                logger.warning("Deprecated (pylatexenc 2.0): "
                               "The `macro_dict=...` option in LatexWalker() is obsolete since "
                               "pylatexenc 2.  It'll still work, but please consider using instead "
                               "the more versatile option `latex_context=...`.")

                macro_dict = kwargs.pop('macro_dict', None)

                default_latex_context = get_default_latex_context_db()

                latex_context = default_latex_context.filter_context(
                       keep_which=['environments']
                )
                latex_context.add_context_category('custom',
                                                   macro_dict.values(),
                                                   default_latex_context.iter_environment_specs())

            else:
                # default -- use default
                latex_context = get_default_latex_context_db()

        else:
            # make sure the user didn't also provide a macro_dict= argument
            if 'macro_dict' in kwargs:
                raise TypeError("Cannot specify both `latex_context=` and `macro_dict=` arguments")

        self.latex_context = latex_context


        #
        # now parsing flags:
        #
        self.tolerant_parsing = kwargs.pop('tolerant_parsing', True)
        self.strict_braces = kwargs.pop('strict_braces', False)

        if 'keep_inline_math' in kwargs:
            logger.warning("Deprecated (pylatexenc 2.0): "
                           "The keep_inline_math=... option in LatexWalker() has no effect "
                           "in pylatexenc 2.  Please use the more versatile option "
                           "math_mode=... in LatexNodes2Text() instead.")
            del kwargs['keep_inline_math']

        if kwargs:
            # any flags left which we haven't recognized
            logger.warning("LatexWalker(): Unknown flag(s) encountered: %r", kwargs.keys())


        #
        # Create the parsed_context object
        #
        self.parsed_context = ParsedContext(
            s=self.s,
            latex_context=self.latex_context,
        )

        super(LatexWalker, self).__init__()


    def parse_flags(self):
        """
        The parse flags currently set on this object.  Returns a dictionary with
        keys 'keep_inline_math', 'tolerant_parsing' and 'strict_braces'.

        .. deprecated:: 2.0

           The 'keep_inline_math' key is always set to `None` starting in
           `pylatexenc 2` and might be removed entirely in future versions.
        """
        return {
            'tolerant_parsing': self.tolerant_parsing,
            'strict_braces': self.strict_braces,
            # compatibility with pylatexenc < 2
            'keep_inline_math': None,
        }
        
    def get_token(self, pos, brackets_are_chars=True, environments=True,
                  keep_inline_math=None, parsing_context=ParsingContext()):
        r"""
        Parses the latex content given to the constructor (and stored in `self.s`),
        starting at position `pos`, to parse a single "token", as defined by
        :py:class:`LatexToken`.

        Parse the token in the stream pointed to at position `pos`.

        For tokens of type 'char', usually a single character is returned.  The
        only exception is at paragraph boundaries, where a single 'char'-type
        token has argument '\\n\\n'.

        Returns a :py:class:`LatexToken`. Raises
        :py:exc:`LatexWalkerEndOfStream` if end of stream reached.

        If `brackets_are_chars=False`, then square bracket characters count as
        'brace_open' and 'brace_close' token types (see :py:class:`LatexToken`);
        otherwise (the default) they are considered just like other normal
        characters.

        If `environments=False`, then ``\begin`` and ``\end`` tokens count as
        regular 'macro' tokens (see :py:class:`LatexToken`); otherwise (the
        default) they are considered as the token types 'begin_environment' and
        'end_environment'.

        The parsing of the tokens might be influcenced by the `parsing_context`
        (a :py:class:`ParsingContext` instance).  Currently, the only influence
        this has is that some latex specials are parsed differently if in math
        mode.  See doc for :py:class:`ParsingContext`.

        .. deprecated:: 2.0

           The flag `keep_inline_math` is only accepted for compatibiltiy with
           earlier versions of `pylatexenc`, but it has no effect starting in
           `pylatexenc 2`.  See the :py:class:`LatexWalker` class doc.

        .. versionadded:: 2.0

           The `parsing_context` argument was introduced in version 2.0.
        """

        s = self.s # shorthand

        space = ''
        while pos < len(s) and s[pos].isspace():
            space += s[pos]
            pos += 1
            if space.endswith('\n\n'):  # two \n's indicate new paragraph.
                return LatexToken(tok='char', arg='\n\n', pos=pos-2, len=2, pre_space=space)

        if pos >= len(s):
            raise LatexWalkerEndOfStream()

        if s[pos] == '\\':
            # escape sequence
            if pos+1 >= len(s):
                raise LatexWalkerEndOfStream()
            macro = s[pos+1] # next char is necessarily part of macro
            # following chars part of macro only if all are alphabetical
            isalphamacro = False
            i = 2
            if s[pos+1].isalpha():
                isalphamacro = True
                while pos+i<len(s) and s[pos+i].isalpha():
                    macro += s[pos+i]
                    i += 1

            # special treatment for \( ... \) and \[ ... \] -- "macros" for
            # inline/display math modes
            if macro in ['[', ']']:
                return LatexToken(tok='mathmode_display', arg='\\'+macro,
                                  pos=pos, len=i, pre_space=space)
            if macro in ['(', ')']:
                return LatexToken(tok='mathmode_inline', arg='\\'+macro,
                                  pos=pos, len=i, pre_space=space)

            # see if we have a begin/end environment
            if environments and macro in ['begin', 'end']:
                # \begin{environment} or \end{environment}
                envmatch = re.match(r'^\s*\{([\w*]+)\}', s[pos+i:])
                if envmatch is None:
                    raise LatexWalkerParseError(
                        s=s,
                        pos=pos,
                        msg=r"Bad \{} macro: expected {{<environment-name>}}".format(macro)
                    )

                return LatexToken(
                    tok=('begin_environment' if macro == 'begin' else 'end_environment'),
                    arg=envmatch.group(1),
                    pos=pos,
                    len=i+envmatch.end(), # !!envmatch.end() counts from pos+i
                    pre_space=space
                    )

            # get the following whitespace, and store it in the macro's post_space
            post_space = ''
            if isalphamacro:
                # important, LaTeX does not consume space after non-alpha macros, like \&
                while pos+i<len(s) and s[pos+i].isspace():
                    post_space += s[pos+i]
                    i += 1

            return LatexToken(tok='macro', arg=macro, pos=pos, len=i,
                              pre_space=space, post_space=post_space)

        if s[pos] == '%':
            # latex comment
            m = re.search(r'(\n|\r|\n\r)\s*', s[pos:])
            mlen = None
            if m is not None:
                arglen = m.start() # relative to pos already
                mlen = m.end() # relative to pos already
                mspace = m.group()
            else:
                arglen = len(s)-pos# [  ==len(s[pos:])  ]
                mlen = arglen
                mspace = ''
            return LatexToken(tok='comment', arg=s[pos+1:pos+arglen], pos=pos, len=mlen,
                              pre_space=space, post_space=mspace)

        openbracechars = '{'
        closebracechars = '}'
        if not brackets_are_chars:
            openbracechars += '['
            closebracechars += ']'

        if s[pos] in openbracechars:
            return LatexToken(tok='brace_open', arg=s[pos], pos=pos, len=1, pre_space=space)

        if s[pos] in closebracechars:
            return LatexToken(tok='brace_close', arg=s[pos], pos=pos, len=1, pre_space=space)

        # check for math-mode dollar signs.  Using python syntax "string.startswith(pattern, pos)"
        if s.startswith('$$', pos):
            return LatexToken(tok='mathmode_display', arg='$$', pos=pos, len=2, pre_space=space)
        if s.startswith('$', pos):
            return LatexToken(tok='mathmode_inline', arg='$', pos=pos, len=1, pre_space=space)

        sspec = self.latex_context.test_for_specials(s, pos, parsing_context=parsing_context)
        if sspec is not None:
            return LatexToken(tok='specials', arg=sspec,
                              pos=pos, len=len(sspec.specials_chars), pre_space=space)

        # otherwise, the token is a normal 'char' type.

        return LatexToken(tok='char', arg=s[pos], pos=pos, len=1, pre_space=space)


    def _mknode(self, nclass, **kwargs):
        assert 'pos' in kwargs and 'len' in kwargs
        return nclass(parsed_context=self.parsed_context, **kwargs)

    def _mknodeposlen(self, nclass, **kwargs):
        return ( self._mknode(nclass, **kwargs), kwargs['pos'], kwargs['len'] )


    def get_latex_expression(self, pos, strict_braces=None, parsing_context=ParsingContext()):
        r"""
        Parses the latex content given to the constructor (and stored in `self.s`),
        starting at position `pos`, to parse a single LaTeX expression.

        Reads a latex expression, e.g. macro argument. This may be a single char, an escape
        sequence, or a expression placed in braces.  This is what TeX calls a "token" (and
        not what we call a token... anyway).

        Parsing might be influenced by the `parsing_context`.  See doc for
        :py:class:`ParsingContext`.

        Returns a tuple `(node, pos, len)`, where `pos` is the position of the
        first char of the expression and `len` the length of the expression.

        .. versionadded:: 2.0

           The `parsing_context` argument was introduced in version 2.0.
        """

        with _PushPropOverride(self, 'strict_braces', strict_braces):

            tok = self.get_token(pos, environments=False, parsing_context=parsing_context)

            if tok.tok == 'macro':
                if tok.arg == 'end':
                    if not self.tolerant_parsing:
                        # error, we were expecting a single token
                        raise LatexWalkerParseError(r"Expected expression, got \end", self.s, pos)
                    else:
                        return self._mknodeposlen(LatexCharsNode, chars='', pos=tok.pos, len=0)
                return self._mknodeposlen(LatexMacroNode, macroname=tok.arg,
                                          nodeargd=None,
                                          macro_post_space=tok.post_space,
                                          nodeoptarg=None, nodeargs=None,
                                          pos=tok.pos, len=tok.len)
            if tok.tok == 'specials':
                return self._mknodeposlen(LatexSpecialsNode, specials_chars=tok.arg.specials_chars,
                                          nodeargd=None,
                                          pos=tok.pos, len=tok.len)
            if tok.tok == 'comment':
                return self.get_latex_expression(tok.pos+tok.len, parsing_context=parsing_context)
            if tok.tok == 'brace_open':
                return self.get_latex_braced_group(tok.pos, parsing_context=parsing_context)
            if tok.tok == 'brace_close':
                if self.strict_braces and not self.tolerant_parsing:
                    raise LatexWalkerParseError(
                        "Expected expression, got closing brace '{}'".format(tok.arg),
                        self.s, pos
                    )
                return self._mknodeposlen(LatexCharsNode, chars='', pos=tok.pos, len=0)
            if tok.tok == 'char':
                return self._mknodeposlen(LatexCharsNode, chars=tok.arg, pos=tok.pos, len=tok.len)
            if tok.tok in ('mathmode_inline', 'mathmode_display'):
                # don't report a math mode token, treat as char or macro
                if tok.arg.startswith('\\'):
                    return self._mknodeposlen(LatexMacroNode, macroname=tok.arg,
                                              nodeoptarg=None, nodeargs=None,
                                              macro_post_space=tok.post_space,
                                              pos=tok.pos, len=tok.len)
                else:
                    return self._mknodeposlen(LatexCharsNode, chars=tok.arg, pos=tok.pos, len=tok.len)

            raise LatexWalkerParseError("Unknown token type: {}".format(tok.tok), self.s, pos)


    def get_latex_maybe_optional_arg(self, pos, parsing_context=ParsingContext()):
        r"""
        Parses the latex content given to the constructor (and stored in `self.s`),
        starting at position `pos`, to attempt to parse an optional argument.

        Parsing might be influenced by the `parsing_context`. See doc for
        :py:class:`ParsingContext`.

        Attempts to parse an optional argument. If this is successful, we return
        a tuple `(node, pos, len)` if success where `node` is a
        :py:class:`LatexGroupNode`.  Otherwise, this method returns None.

        .. versionadded:: 2.0

           The `parsing_context` argument was introduced in version 2.0.
        """

        tok = self.get_token(pos, brackets_are_chars=False, environments=False,
                             parsing_context=parsing_context)
        if (tok.tok == 'brace_open' and tok.arg == '['):
            return self.get_latex_braced_group(pos, brace_type='[',
                                               parsing_context=parsing_context)

        return None


    def get_latex_braced_group(self, pos, brace_type='{',
                               parsing_context=ParsingContext()):
        r"""
        Parses the latex content given to the constructor (and stored in `self.s`),
        starting at position `pos`, to read a latex group delimited by braces.

        Reads a latex expression enclosed in braces ``{ ... }``. The first token of
        `s[pos:]` must be an opening brace.

        Parsing might be influenced by the `parsing_context`.  See doc for
        :py:class:`ParsingContext`.

        Returns a tuple `(node, pos, len)`, where `node` is a
        :py:class:`LatexGroupNode` instance, `pos` is the position of the first
        char of the expression (which has to be an opening brace), and `len` is
        the length of the group, including the closing brace (relative to the
        starting position).

        .. versionadded:: 2.0

           The `parsing_context` argument was introduced in version 2.0.
        """

        closing_brace = None
        if (brace_type == '{'):
            closing_brace = '}'
        elif (brace_type == '['):
            closing_brace = ']'
        else:
            raise LatexWalkerParseError(s=self.s, pos=pos, msg="Uknown brace type: %s" %(brace_type))
        brackets_are_chars = (brace_type != '[')

        firsttok = self.get_token(pos, brackets_are_chars=brackets_are_chars,
                                  parsing_context=parsing_context)
        if firsttok.tok != 'brace_open'  or  firsttok.arg != brace_type:
            raise LatexWalkerParseError(
                s=self.s,
                pos=pos,
                msg='get_latex_braced_group: not an opening brace/bracket: %s' %(self.s[pos])
            )

        (nodelist, npos, nlen) = self.get_latex_nodes(
            firsttok.pos + firsttok.len,
            stop_upon_closing_brace=closing_brace,
            parsing_context=parsing_context
        )

        return self._mknodeposlen(LatexGroupNode, nodelist=nodelist,
                                  pos = firsttok.pos,
                                  len = npos + nlen - firsttok.pos)


    def get_latex_environment(self, pos, environmentname=None,
                              parsing_context=ParsingContext()):
        r"""
        Parses the latex content given to the constructor (and stored in `self.s`),
        starting at position `pos`, to read a latex environment.

        Reads a latex expression enclosed in a
        ``\begin{environment}...\end{environment}``.  The first token in the
        stream must be the ``\begin{environment}``.

        If `environmentname` is given and nonempty, then additionally a
        :py:exc:`LatexWalkerParseError` is raised if the environment in the
        input stream does not match the provided environment name.

        Arguments to the begin environment command are parsed according to the
        corresponding specification in the given latex context `latex_context`
        provided to the constructor.  The environment name is looked up as a
        "macro name" in the macro spec.

        Parsing might be influenced by the `parsing_context`.  See doc for
        :py:class:`ParsingContext`.

        Returns a tuple (node, pos, len) where node is a
        :py:class:`LatexEnvironmentNode`.

        .. versionadded:: 2.0

           The `parsing_context` argument was introduced in version 2.0.
        """

        startpos = pos

        firsttok = self.get_token(pos, parsing_context=parsing_context)
        if firsttok.tok != 'begin_environment'  or  \
           (environmentname is not None and firsttok.arg != environmentname):
            raise LatexWalkerParseError(s=self.s, pos=pos,
                                        msg=r'get_latex_environment: expected \begin{%s}: %s' %(
                environmentname if environmentname is not None else '<environment name>',
                firsttok.arg
                ))
        if (environmentname is None):
            environmentname = firsttok.arg

        pos = firsttok.pos + firsttok.len

        env_spec = self.latex_context.get_environment_spec(environmentname)
        if env_spec is None:
            env_spec = macrospec.EnvironmentSpec('')

        # self = latex walker instance
        (argd, apos, alen) = env_spec.parse_args(w=self, pos=pos, parsing_context=parsing_context)

        pos = apos + alen

        parsing_context_inner = parsing_context
        if env_spec.is_math_mode:
            parsing_context_inner = parsing_context.sub_context(in_math_mode=True)

        (nodelist, npos, nlen) = self.get_latex_nodes(pos,
                                                      stop_upon_end_environment=environmentname,
                                                      parsing_context=parsing_context_inner)

        if argd.legacy_nodeoptarg_nodeargs:
            legnodeoptarg = argd.legacy_nodeoptarg_nodeargs[0]
            legnodeargs = argd.legacy_nodeoptarg_nodeargs[1]
        else:
            legnodeoptarg, legnodeargs = None, []

        return self._mknodeposlen(LatexEnvironmentNode,
                                  envname=environmentname,
                                  nodelist=nodelist,
                                  nodeargd=argd,
                                  # legacy:
                                  optargs=[legnodeoptarg],
                                  args=legnodeargs,
                                  pos=startpos,
                                  len=npos+nlen-startpos)


    

    def get_latex_nodes(self, pos=0, stop_upon_closing_brace=None, stop_upon_end_environment=None,
                        stop_upon_closing_mathmode=None, read_max_nodes=None,
                        parsing_context=ParsingContext()):
        r"""
        Parses the latex content given to the constructor (and stored in `self.s`)
        into a list of nodes.

        Returns a tuple `(nodelist, pos, len)` where:

          - `nodelist` is a list of :py:class:`LatexNode`\ 's representing the
            parsed LaTeX code.

          - `pos` is the same as the `pos` given as argument; if there is
            leading whitespace it is reported in `nodelist` using a
            :py:class:`LatexCharsNode`.

          - `len` is the length of the parsed expression.  If one of the
            `stop_upon_...=` arguments are provided (cf below), then the `len`
            includes the length of the token/expression that stopped the
            parsing.
        
        If `stop_upon_closing_brace` is given and set to a character, then
        parsing stops once the given closing brace is encountered (but not
        inside a subgroup).  The brace is given as a character, ']' or '}'.  The
        returned `len` includes the closing brace, but the closing brace is not
        included in any of the nodes in the `nodelist`.

        If `stop_upon_end_environment` is provided, then parsing stops once the
        given environment was closed.  If there is an environment mismatch, then
        a `LatexWalkerParseError` is raised except in tolerant parsing mode (see
        :py:meth:`parse_flags()`).  Again, the closing environment is included
        in the length count but not the nodes.

        If `stop_upon_closing_mathmode` is specified, then the parsing stops
        once the corresponding math mode (assumed already open) is closed.  This
        argument may take the values `None` (no particular request to stop at
        any math mode token), or one of ``$``, ``$$``, ``\)`` or ``\]``
        indicating a closing math mode delimiter that we are expecting and at
        which point parsing should stop.

        If the token '$' (respectively '$$') is encountered, it is interpreted
        as the *beginning* of a new math mode chunk *unless* the argument
        `stop_upon_closing_mathmode=...` has been set to '$' (respectively
        '$$').

        If `read_max_nodes` is non-`None`, then it should be set to an integer
        specifying the maximum number of top-level nodes to read before
        returning.  (Top-level nodes means that macro arguments, environment or
        group contents, etc., do not count towards `read_max_nodes`.)  If
        `None`, the entire input string will be parsed.

        .. note::

           There are a few important differences between
           ``get_latex_nodes(read_max_nodes=1)`` and ``get_latex_expression()``:
           The former reads a logical node of the LaTeX document, which can be a
           sequence of characters, a macro invokation with arguments, or an
           entire environment, but the latter reads a single LaTeX "token" in
           the same way as LaTeX parses macro arguments.

           For instance, if a macro is encountered, then
           ``get_latex_nodes(read_max_nodes=1)`` will read and parse its
           arguments, and include it in the corresponding
           :py:class:`LatexMacroNode`, whereas ``get_latex_expression()`` will
           return a minimal :py:class:`LatexMacroNode` with no arguments
           regardless of the macro's argument specification.  The same holds for
           latex specials.  For environments,
           ``get_latex_nodes(read_max_nodes=1)`` will return the entire parsed
           environment into a :py:class:`LatexEnvironmentNode`, whereas
           ``get_latex_expression()`` will return a :py:class:`LatexMacroNode`
           named 'begin' with no arguments.

        Parsing might be influenced by the `parsing_context`.  See doc for
        :py:class:`ParsingContext`.

        .. versionadded:: 2.0

           The `parsing_context` argument was introduced in version 2.0.
        """

        nodelist = []
    
        brackets_are_chars = True
        if (stop_upon_closing_brace == ']'):
            brackets_are_chars = False

        # consistency check
        if stop_upon_closing_mathmode is not None and not parsing_context.in_math_mode:
            logger.warning(("Call to LatexWalker.get_latex_nodes(stop_upon_closing_mathmode={!r}) "
                            "but parsing context has in_math_mode={!r}").format(
                                stop_upon_closing_mathmode,
                                parsing_context.in_math_mode,
                            ))

        origpos = pos

        class PosPointer:
            def __init__(self, pos=0, lastchars='', lastchars_pos=None):
                self.pos = pos
                self.lastchars = lastchars
                self.lastchars_pos = lastchars_pos

        p = PosPointer(pos=pos, lastchars='', lastchars_pos=None)

        def do_read(nodelist, p):
            r"""
            Read a single token and process it, recursing into brace blocks and
            environments etc if needed, and appending stuff to nodelist.

            Return True whenever we should stop trying to read more. (e.g. upon
            reaching the a matched stop_upon_end_environment etc.)
            """

            try:
                tok = self.get_token(p.pos, brackets_are_chars=brackets_are_chars,
                                     parsing_context=parsing_context)
            except LatexWalkerEndOfStream:
                if self.tolerant_parsing:
                    return True
                raise # re-raise

            p.pos = tok.pos + tok.len

            # if it's a char, just append it to the stream of last characters.
            if tok.tok == 'char':
                p.lastchars += tok.pre_space + tok.arg
                if p.lastchars_pos is None:
                    p.lastchars_pos = tok.pos - len(tok.pre_space)
                return False

            # if it's not a char, push the last `p.lastchars` into the node list
            # before we do anything else
            if len(p.lastchars):
                strnode = self._mknode(LatexCharsNode, chars=p.lastchars+tok.pre_space,
                                       pos=p.lastchars_pos, len=tok.pos - p.lastchars_pos)
                p.lastchars = ''
                p.lastchars_pos = None
                nodelist.append(strnode)
                if read_max_nodes and len(nodelist) >= read_max_nodes:
                    # adjust p.pos for return value of get_latex_nodes()
                    p.pos = tok.pos
                    return True
            elif len(tok.pre_space):
                # If we have pre_space, add a separate chars node that contains
                # the spaces.  We do this seperately, so that latex2text can
                # ignore these groups by default to avoid too much space on the
                # output.  This allows latex2text to implement the
                # `strict_latex_spaces=True` flag correctly.
                spacestrnode = self._mknode(LatexCharsNode, chars=tok.pre_space,
                                            pos=tok.pos-len(tok.pre_space), len=len(tok.pre_space))
                nodelist.append(spacestrnode)
                if read_max_nodes and len(nodelist) >= read_max_nodes:
                    return True

            # and see what the token is.

            if tok.tok == 'brace_close':
                # we've reached the end of the group. stop the parsing.
                if tok.arg != stop_upon_closing_brace:
                    raise LatexWalkerParseError(
                        s=self.s,
                        pos=tok.pos,
                        msg="Unexpected mismatching closing brace: '%s'"%(tok.arg)
                    )
                return True

            if tok.tok == 'end_environment':
                # we've reached the end of an environment.
                if tok.arg != stop_upon_end_environment:
                    raise LatexWalkerParseError(
                        s=self.s,
                        pos=tok.pos,
                        msg=("Unexpected mismatching closing environment: '{}', "
                             "was expecting '{}'".format(tok.arg, stop_upon_end_environment))
                    )
                return True

            if tok.tok in ('mathmode_inline', 'mathmode_display'):
                # see if we need to stop at a math mode 
                if stop_upon_closing_mathmode is not None:
                    if tok.arg == stop_upon_closing_mathmode:
                        # all OK, found the closing mathmode.
                        return True
                    if tok.arg in [r'\)', r'\]']:
                        # this is definitely a closing math-mode delimiter, so
                        # not a new math mode block.  This is a parse error,
                        # because we need to match the given
                        # stop_upon_closing_mathmode mode.
                        raise LatexWalkerParseError(
                            s=self.s,
                            pos=tok.pos,
                            msg="Mismatching closing math mode: '{}', expected '{}'".format(
                                tok.arg, stop_upon_closing_mathmode,
                            )
                        )
                    # all ok, this is a new math mode opening.  Keep an assert
                    # in case we forget to include some math-mode delimiters in
                    # the future.
                    assert tok.arg in ['$', '$$', r'\(', r'\[']
                elif tok.arg in [r'\)', r'\]']:
                    # unexpected close-math-mode delimiter, but no
                    # stop_upon_closing_mathmode was specified. Parse error.
                    raise LatexWalkerParseError(
                            s=self.s,
                            pos=tok.pos,
                            msg="Unexpected closing math mode: '{}'".format(
                                tok.arg,
                            )
                    )

                # we have encountered a new math inline, parse the math expression

                corresponding_closing_mathmode = {r'\(': r'\)', r'\[': r'\]'}.get(tok.arg, tok.arg)
                displaytype = 'inline' if tok.arg in [r'\(', '$'] else 'display'

                parsing_context_inner = parsing_context.sub_context(in_math_mode=True)

                (mathinline_nodelist, mpos, mlen) = self.get_latex_nodes(
                    p.pos,
                    stop_upon_closing_mathmode=corresponding_closing_mathmode,
                    parsing_context=parsing_context_inner
                )
                p.pos = mpos + mlen

                nodelist.append(self._mknode(LatexMathNode, displaytype=displaytype,
                                             nodelist=mathinline_nodelist,
                                             delimiters=(tok.arg, corresponding_closing_mathmode),
                                             pos=tok.pos, len=mpos+mlen-tok.pos))
                if read_max_nodes and len(nodelist) >= read_max_nodes:
                    return True
                return

            if tok.tok == 'comment':
                commentnode = self._mknode(LatexCommentNode, comment=tok.arg,
                                           comment_post_space=tok.post_space,
                                           pos=tok.pos, len=tok.len)
                nodelist.append(commentnode)
                if read_max_nodes and len(nodelist) >= read_max_nodes:
                    return True
                return

            if tok.tok == 'brace_open':
                # another braced group to read.
                (groupnode, bpos, blen) = self.get_latex_braced_group(tok.pos,
                                                                      parsing_context=parsing_context)
                p.pos = bpos + blen
                nodelist.append(groupnode)
                if read_max_nodes and len(nodelist) >= read_max_nodes:
                    return True
                return

            if tok.tok == 'begin_environment':
                # an environment to read.
                (envnode, epos, elen) = self.get_latex_environment(tok.pos, environmentname=tok.arg,
                                                                   parsing_context=parsing_context)
                p.pos = epos + elen
                # add node and continue.
                nodelist.append(envnode)
                if read_max_nodes and len(nodelist) >= read_max_nodes:
                    return True
                return

            if tok.tok == 'macro':
                # read a macro. see if it has arguments.
                macroname = tok.arg
                mspec = self.latex_context.get_macro_spec(macroname)
                if mspec is None:
                    mspec = macrospec.MacroSpec('')

                (nodeargd, mapos, malen) = \
                    mspec.parse_args(w=self, pos=tok.pos + tok.len, parsing_context=parsing_context)

                p.pos = mapos + malen

                if nodeargd.legacy_nodeoptarg_nodeargs:
                    nodeoptarg = nodeargd.legacy_nodeoptarg_nodeargs[0]
                    nodeargs = nodeargd.legacy_nodeoptarg_nodeargs[1]
                else:
                    nodeoptarg, nodeargs = None, []
                node = self._mknode(LatexMacroNode,
                                    macroname=tok.arg,
                                    nodeargd=nodeargd,
                                    macro_post_space=tok.post_space,
                                    # legacy data:
                                    nodeoptarg=nodeoptarg,
                                    nodeargs=nodeargs,
                                    pos=tok.pos,
                                    len=p.pos-tok.pos)
                nodelist.append(node)
                if read_max_nodes and len(nodelist) >= read_max_nodes:
                    return True
                return None

            if tok.tok == 'specials':
                # read the specials. see if it expects/has arguments.
                sspec = tok.arg

                p.pos = tok.pos + tok.len
                nodeargd = None

                res = sspec.parse_args(w=self, pos=p.pos, parsing_context=parsing_context)
                if res is not None:
                    # specials expects arguments, read them
                    (nodeargd, mapos, malen) = res

                    p.pos = mapos + malen

                node = self._mknode(LatexSpecialsNode,
                                    specials_chars=sspec.specials_chars,
                                    nodeargd=nodeargd,
                                    pos=tok.pos,
                                    len=p.pos-tok.pos)
                nodelist.append(node)
                if read_max_nodes and len(nodelist) >= read_max_nodes:
                    return True
                return None


            raise LatexWalkerParseError(s=self.s, pos=p.pos, msg="Unknown token: %r" %(tok))



        while True:
            try:
                r_endnow = do_read(nodelist, p)
            except LatexWalkerParseError as e:
                if self.tolerant_parsing:
                    logger.debug("Ignoring parse error (tolerant parsing mode): %s", e)
                    r_endnow = False
                else:
                    raise
            except LatexWalkerEndOfStream:
                if stop_upon_closing_brace or stop_upon_end_environment:
                    # unexpected eof
                    if not self.tolerant_parsing:
                        if stop_upon_closing_brace:
                            expecting = "'"+stop_upon_closing_brace+"'"
                        elif stop_upon_end_environment:
                            expecting = r"\end{"+stop_upon_end_environment+"}"
                        raise LatexWalkerError("Unexpected end of stream, was looking for {}"
                                               .format(expecting))
                    else:
                        r_endnow = False
                else:
                    r_endnow = True

            if (r_endnow):
                # add last chars
                if p.lastchars:
                    strnode = self._mknode(LatexCharsNode, chars=p.lastchars,
                                           pos=p.lastchars_pos, len=len(p.lastchars))
                    nodelist.append(strnode)
                return (nodelist, origpos, p.pos - origpos)

        raise LatexWalkerError(                # lgtm [py/unreachable-statement]
            "CONGRATULATIONS !! "
            "You are the first human to telepathically break an infinite loop !!!!!!!"
        )
































    
    
# ------------------------------------------------------------------------------

def get_token(s, pos, brackets_are_chars=True, environments=True, **parse_flags):
    """
    Parse the next token in the stream.

    Returns a `LatexToken`. Raises `LatexWalkerEndOfStream` if end of stream reached.

    .. deprecated:: 1.0
       Please use :py:meth:`LatexWalker.get_token()` instead.
    """
    return LatexWalker(s, **parse_flags).get_token(pos=pos,
                                                   brackets_are_chars=brackets_are_chars,
                                                   environments=environments)


def get_latex_expression(s, pos, **parse_flags):
    """
    Reads a latex expression, e.g. macro argument. This may be a single char, an escape
    sequence, or a expression placed in braces.

    Returns a tuple `(<LatexNode instance>, pos, len)`. `pos` is the first char of the
    expression, and `len` is its length.

    .. deprecated:: 1.0
       Please use :py:meth:`LatexWalker.get_latex_expression()` instead.
    """

    return LatexWalker(s, **parse_flags).get_latex_expression(pos=pos)


def get_latex_maybe_optional_arg(s, pos, **parse_flags):
    """
    Attempts to parse an optional argument. Returns a tuple `(groupnode, pos, len)` if
    success, otherwise returns None.

    .. deprecated:: 1.0
       Please use :py:meth:`LatexWalker.get_latex_maybe_optional_arg()` instead.
    """

    return LatexWalker(s, **parse_flags).get_latex_maybe_optional_arg(pos=pos)

    
def get_latex_braced_group(s, pos, brace_type='{', **parse_flags):
    """
    Reads a latex expression enclosed in braces {...}. The first token of `s[pos:]` must
    be an opening brace.

    Returns a tuple `(node, pos, len)`. `pos` is the first char of the
    expression (which has to be an opening brace), and `len` is its length,
    including the closing brace.

    .. deprecated:: 1.0
       Please use :py:meth:`LatexWalker.get_latex_braced_group()` instead.
    """

    return LatexWalker(s, **parse_flags).get_latex_braced_group(pos=pos, brace_type=brace_type)


def get_latex_environment(s, pos, environmentname=None, **parse_flags):
    """
    Reads a latex expression enclosed in a \\begin{environment}...\\end{environment}. The first
    token in the stream must be the \\begin{environment}.

    Returns a tuple (node, pos, len) with node being a :py:class:`LatexEnvironmentNode`.

    .. deprecated:: 1.0
       Please use :py:meth:`LatexWalker.get_latex_environment()` instead.
    """

    return LatexWalker(s, **parse_flags).get_latex_environment(pos=pos,
                                                               environmentname=environmentname)

def get_latex_nodes(s, pos=0, stop_upon_closing_brace=None, stop_upon_end_environment=None,
                    stop_upon_closing_mathmode=None, **parse_flags):
    """
    Parses latex content `s`.

    Returns a tuple `(nodelist, pos, len)` where nodelist is a list of `LatexNode` 's.

    If `stop_upon_closing_brace` is given, then `len` includes the closing brace, but the
    closing brace is not included in any of the nodes in the `nodelist`.

    .. deprecated:: 1.0
       Please use :py:meth:`LatexWalker.get_latex_nodes()` instead.
    """

    return LatexWalker(s, **parse_flags).get_latex_nodes(
        stop_upon_closing_brace=stop_upon_closing_brace,
        stop_upon_end_environment=stop_upon_end_environment,
        stop_upon_closing_mathmode=stop_upon_closing_mathmode
    )










# ------------------------------------------------------------------------------

#
# small utilities for displaying & debugging
#


def nodelist_to_latex(nodelist):
    return "".join(n.latex_verbatim() for n in nodelist)



def disp_node(n, indent=0, context='* ', skip_group=False):
    title = ''
    comment = ''
    iterchildren = []
    if n is None:
        title = '<None>'
    elif n.isNodeType(LatexCharsNode):
        title = "'%s'" %(n.chars) #.strip())
    elif n.isNodeType(LatexMacroNode):
        title = '\\'+n.macroname
        #comment = 'opt arg?: %d; %d args' % (n.arg.nodeoptarg is not None, len(n.arg.nodeargs))
        # FIXME: handle more general case with n.nodeargd
        if n.nodeoptarg:
            iterchildren.append(('[...]: ', [n.nodeoptarg], False))
        if len(n.nodeargs):
            iterchildren.append(('{...}: ', n.nodeargs, False))
    elif n.isNodeType(LatexCommentNode):
        title = '%' + n.comment.strip()
    elif n.isNodeType(LatexGroupNode):
        if (skip_group):
            for nn in n.arg:
                disp_node(nn, indent=indent, context=context)
            return
        title = 'Group: '
        iterchildren.append(('* ', n.nodelist, False))
    elif n.isNodeType(LatexEnvironmentNode):
        title = '\\begin{%s}' %(n.envname)
        iterchildren.append(('* ', n.nodelist, False))
    elif n.isNodeType(LatexMathNode):
        title = '$inline math$'
        iterchildren.append(('* ', n.nodelist, False))
    else:
        print("UNKNOWN NODE TYPE: %s"%(n.nodeType().__name__))

    print(' '*indent + context + title + '  '+comment)

    for context, nodelist, skip in iterchildren:
        for nn in nodelist:
            disp_node(nn, indent=indent+4, context=context, skip_group=skip)


class LatexNodesJSONEncoder(json.JSONEncoder):
    """
    A :py:class:`json.JSONEncoder` that can encode :py:class:`LatexNode` objects
    (and subclasses).
    """
    def default(self, obj):
        if isinstance(obj, LatexNode):
            # Prepare a dictionary with the correct keys and values.
            n = obj
            d = {
                'nodetype': n.__class__.__name__,
            }
            #redundant_fields = getattr(n, '_redundant_fields', n._fields)
            for fld in n._fields:
                d[fld] = n.__dict__[fld]
            return d

        if isinstance(obj, macrospec.ParsedMacroArgs):
            return obj.to_json_object()

        # else:
        return super(LatexNodesJSONEncoder, self).default(obj)



def main(argv=None):
    import fileinput
    import argparse

    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser()

    parser.add_argument('--output-format', metavar="FORMAT", dest="output_format",
                        choices=["human", "json"], default='human',
                        help='Requested output format for the node tree')
    parser.add_argument('--json-indent', metavar="NUMSPACES", dest="json_indent",
                        type=int, default=2,
                        help='Indentation in JSON output (specify number of spaces per indentation level)')
    parser.add_argument('--json-compact', dest="json_indent", default=2,
                        action='store_const', const=None,
                        help='Output compact JSON')

    parser.add_argument('--keep-inline-math', action='store_const', const=True,
                        dest='keep_inline_math', default=True,
                        help=argparse.SUPPRESS)
    parser.add_argument('--no-keep-inline-math', action='store_const', const=False,
                        dest='keep_inline_math',
                        help=argparse.SUPPRESS)

    parser.add_argument('--tolerant-parsing', action='store_const', const=True,
                        dest='tolerant_parsing', default=True)
    parser.add_argument('--no-tolerant-parsing', action='store_const', const=False,
                        dest='tolerant_parsing',
                        help="Tolerate syntax errors when parsing, and attempt to continue (default yes)")

    parser.add_argument('--strict-braces', action='store_const', const=True,
                        dest='strict_braces', default=False)
    parser.add_argument('--no-strict-braces', action='store_const', const=False,
                        dest='strict_braces',
                        help="Report errors for mismatching LaTeX braces (default no)")

    parser.add_argument('files', metavar="FILE", nargs='*',
                        help='Input files (if none specified, read from stdandard input)')

    args = parser.parse_args(argv)

    latex = ''
    for line in fileinput.input(files=args.files):
        latex += line
    
    latexwalker = LatexWalker(latex,
                              tolerant_parsing=args.tolerant_parsing,
                              strict_braces=args.strict_braces)

    (nodelist, pos, len_) = latexwalker.get_latex_nodes()

    if args.output_format == 'human':
        print('\n--- NODES ---\n')
        for n in nodelist:
            disp_node(n)
        print('\n-------------\n')
        return

    if args.output_format == 'json':
        json.dump({ 'nodelist': nodelist, },
                  sys.stdout,
                  cls=LatexNodesJSONEncoder,
                  indent=args.json_indent)
        sys.stdout.write("\n")
        return
    
    raise ValueError("Invalid output format: "+args.output_format)


def run_main():

    try:

        main()

    except SystemExit:
        raise
    except: # lgtm [py/catch-base-exception]
        import pdb
        import traceback
        traceback.print_exc()
        pdb.post_mortem()


if __name__ == '__main__':

    run_main()
