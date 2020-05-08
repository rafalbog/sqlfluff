"""Defines the templaters."""

import ast

from .errors import SQLTemplaterError
from .parser import FilePositionMarker

_templater_lookup = {}


def templater_selector(s=None, **kwargs):
    """Instantitate a new templater by name."""
    s = s or 'jinja'  # default to jinja
    try:
        cls = _templater_lookup[s]
        # Instantiate here, optionally with kwargs
        return cls(**kwargs)
    except KeyError:
        raise ValueError(
            "Requested templater {0!r} which is not currently available. Try one of {1}".format(
                s, ', '.join(_templater_lookup.keys())
            ))


def register_templater(cls):
    """Register a new templater by name.

    This is designed as a decorator for templaters.

    e.g.
    @register_templater()
    class RawTemplateInterface(BaseSegment):
        blah blah blah

    """
    n = cls.name
    _templater_lookup[n] = cls
    return cls


@register_templater
class RawTemplateInterface:
    """A templater which does nothing.

    This also acts as the base templating class.
    """

    name = 'raw'
    templater_selector = 'templater'

    def __init__(self, **kwargs):
        """Placeholder init function.

        Here we should load any initial config found in the root directory. The init
        function shouldn't take any arguments at this stage as we assume that it will load
        it's own config. Maybe at this stage we might allow override parameters to be passed
        to the linter at runtime from the cli - that would be the only time we would pass
        arguments in here.
        """

    @staticmethod
    def process(in_str, fname=None, config=None):
        """Process a string and return the new string.

        Args:
            in_str (:obj:`str`): The input string.
            fname (:obj:`str`, optional): The filename of this string. This is
                mostly for loading config files at runtime.
            config (:obj:`FluffConfig`): A specific config to use for this
                templating operation. Only necessary for some templaters.

        """
        return in_str, []

    def __eq__(self, other):
        """Return true if `other` is of the same class as this one.

        NB: This is useful in comparing configs.
        """
        return isinstance(other, self.__class__)


@register_templater
class PythonTemplateInterface(RawTemplateInterface):
    """A templater using python format strings.

    See: https://docs.python.org/3/library/string.html#format-string-syntax

    For the python templater we don't allow functions or macros because there isn't
    a good way of doing it securely. Use the jinja templater for this.
    """

    name = 'python'

    def __init__(self, override_context=None, **kwargs):
        self.default_context = dict(test_value='__test__')
        self.override_context = override_context or {}

    @staticmethod
    def infer_type(s):
        """Infer a python type from a string ans convert.

        Given a string value, convert it to a more specific built-in Python type
        (e.g. int, float, list, dictionary) if possible.

        """
        try:
            return ast.literal_eval(s)
        except (SyntaxError, ValueError):
            return s

    def get_context(self, fname=None, config=None):
        """Get the templating context from the config."""
        # TODO: The config loading should be done outside the templater code. Here
        # is a silly place.
        if config:
            # This is now a nested section
            loaded_context = config.get_section((self.templater_selector, self.name, 'context')) or {}
        else:
            loaded_context = {}
        live_context = {}
        live_context.update(self.default_context)
        live_context.update(loaded_context)
        live_context.update(self.override_context)

        # Infer types
        for k in loaded_context:
            live_context[k] = self.infer_type(live_context[k])
        return live_context

    def process(self, in_str, fname=None, config=None):
        """Process a string and return the new string.

        Args:
            in_str (:obj:`str`): The input string.
            fname (:obj:`str`, optional): The filename of this string. This is
                mostly for loading config files at runtime.
            config (:obj:`FluffConfig`): A specific config to use for this
                templating operation. Only necessary for some templaters.

        """
        live_context = self.get_context(fname=fname, config=config)
        try:
            return in_str.format(**live_context), []
        except KeyError as err:
            # TODO: Add a url here so people can get more help.
            raise SQLTemplaterError(
                "Failure in Python templating: {0}. Have you configured your variables?".format(err))


@register_templater
class JinjaTemplateInterface(PythonTemplateInterface):
    """A templater using the jinja2 library.

    See: https://jinja.palletsprojects.com/
    """

    name = 'jinja'

    @staticmethod
    def _extract_macros_from_template(template, env):
        """Take a template string and extract any macros from it.

        Lovingly inspired by http://codyaray.com/2015/05/auto-load-jinja2-macros
        """
        from jinja2.runtime import Macro  # noqa

        # Iterate through keys exported from the loaded template string
        context = {}
        macro_template = env.from_string(template)
        # This is kind of low level and hacky but it works
        for k in macro_template.module.__dict__:
            attr = getattr(macro_template.module, k)
            # Is it a macro? If so install it at the name of the macro
            if isinstance(attr, Macro):
                context[k] = attr
        # Return the context
        return context

    def _extract_macros_from_config(self, config, env):
        """Take a config and load any macros from it."""
        if config:
            # This is now a nested section
            loaded_context = config.get_section((self.templater_selector, self.name, 'macros')) or {}
        else:
            loaded_context = {}

        # Iterate to load macros
        macro_ctx = {}
        for value in loaded_context.values():
            macro_ctx.update(
                self._extract_macros_from_template(
                    value, env=env
                )
            )
        return macro_ctx

    def process(self, in_str, fname=None, config=None):
        """Process a string and return the new string.

        Args:
            in_str (:obj:`str`): The input string.
            fname (:obj:`str`, optional): The filename of this string. This is
                mostly for loading config files at runtime.
            config (:obj:`FluffConfig`): A specific config to use for this
                templating operation. Only necessary for some templaters.

        """
        # No need to import this unless we're using this templater
        from jinja2.sandbox import SandboxedEnvironment  # noqa
        from jinja2 import meta  # noqa
        import jinja2.nodes  # noqa 
        # We explicitly want to preserve newlines.
        env = SandboxedEnvironment(
            keep_trailing_newline=True,
            # The do extension allows the "do" directive
            autoescape=False, extensions=['jinja2.ext.do']
        )

        ctx = self._extract_macros_from_config(config=config, env=env)
        # Apply to globals
        env.globals.update(ctx)

        template = env.from_string(in_str)
        live_context = self.get_context(fname=fname, config=config)

        violations = []

        # Attempt to identify any undeclared variables
        try:
            ast = env.parse(in_str)
            undefined_variables = meta.find_undeclared_variables(ast)
        except Exception as err:
            # TODO: Add a url here so people can get more help.
            raise SQLTemplaterError(
                "Failure in identifying Jinja variables: {0}.".format(err))

        # Get rid of any that *are* actually defined.
        for val in live_context:
            if val in undefined_variables:
                undefined_variables.remove(val)

        if undefined_variables:
            # Lets go through and find out where they are:
            def _crawl_tree(tree, variable_names, raw):
                """Crawl the tree looking for occurances of the undeclared values."""
                for elem in tree.iter_child_nodes():
                    yield from _crawl_tree(elem, variable_names, raw)
                else:
                    if isinstance(tree, jinja2.nodes.Name) and tree.name in variable_names:
                        line_no = tree.lineno
                        line = raw.split('\n')[line_no - 1]
                        pos = line.index(tree.name) + 1
                        # Generate the charpos. +1 is for the newline characters themselves
                        charpos = sum(len(raw_line) + 1 for raw_line in raw.split('\n')[:line_no - 1]) + pos
                        # NB: The positions returned here will be *inconsistent* with those
                        # from the linter at the moment, because these are references to the
                        # structure of the file *before* templating.
                        yield SQLTemplaterError(
                            "Undefined jinja template variable: {0!r}".format(tree.name),
                            pos=FilePositionMarker(None, line_no, pos, charpos)
                        )

            for val in _crawl_tree(ast, undefined_variables, in_str):
                violations.append(val)

        try:
            out_str = template.render(**live_context)
            return out_str, violations
        except Exception as err:
            # TODO: Add a url here so people can get more help.
            violations.append(
                SQLTemplaterError(
                    ("Unrecoverable failure in Jinja templating: {0}. Have you configured "
                     "your variables? https://docs.sqlfluff.com/en/latest/configuration.html").format(err))
            )
            return None, violations
