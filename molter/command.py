import functools
import inspect
import re
import typing  # it's weird importing get_args and get_origin directly
from contextlib import suppress
from collections import deque
from types import NoneType, UnionType
from typing import Optional, Any, Callable, Sequence, Annotated, Literal, Union, TypeVar

import attrs

from dis_snek.client.const import MISSING
from dis_snek.client.utils.input_utils import _quotes
from dis_snek.client.utils.attr_utils import define, field, docs
from dis_snek.models.snek.command import PrefixedCommand
from dis_snek.models.snek.context import PrefixedContext

from molter.errors import BadArgument
from molter.converters import Converter, LiteralConverter, Greedy, SNEK_OBJECT_TO_CONVERTER

__all__ = (
    "CommandParameter",
    "ArgsIterator",
    "maybe_coroutine",
    "MolterCommand",
    "message_command",
    "msg_command",
    "register_converter",
)

# turns out dis-snek's args thinks newlines are the start of new arguments
# they aren't, polls.
# ...but seriously, we do have to work around this, and i don't want to make
# something only the overrides can do
_pending_regex = r"(1.*2|[^\t\f\v ]+)"
_pending_regex = _pending_regex.replace("1", f"[{''.join(list(_quotes.keys()))}]")
_pending_regex = _pending_regex.replace("2", f"[{''.join(list(_quotes.values()))}]")
ARGS_PARSE = re.compile(_pending_regex)


@attrs.define(slots=True)
class CommandParameter:
    """An object representing parameters in a command."""

    name: str = attrs.field(default=None)
    default: Optional[Any] = attrs.field(default=None)
    type: type = attrs.field(default=None)
    converters: list[Callable[[PrefixedContext, str], Any]] = attrs.field(factory=list)
    greedy: bool = attrs.field(default=False)
    union: bool = attrs.field(default=False)
    variable: bool = attrs.field(default=False)
    consume_rest: bool = attrs.field(default=False)

    @property
    def optional(self) -> bool:
        return self.default != MISSING


@attrs.define(slots=True)
class ArgsIterator:
    """
    An iterator over the arguments of a command.

    Has functions to control the iteration.
    """

    args: Sequence[str] = attrs.field(converter=tuple)
    index: int = attrs.field(init=False, default=0)
    length: int = attrs.field(init=False, default=0)

    def __iter__(self) -> "ArgsIterator":
        self.length = len(self.args)
        return self

    def __next__(self) -> str:
        if self.index >= self.length:
            raise StopIteration

        result = self.args[self.index]
        self.index += 1
        return result

    def consume_rest(self) -> Sequence[str]:
        result = self.args[self.index - 1 :]
        self.index = self.length
        return result

    def back(self, count: int = 1) -> None:
        self.index -= count

    def reset(self) -> None:
        self.index = 0

    @property
    def finished(self) -> bool:
        return self.index >= self.length


def _get_name(x: Any) -> str:
    try:
        return x.__name__
    except AttributeError:
        return repr(x) if hasattr(x, "__origin__") else x.__class__.__name__


def _convert_to_bool(argument: str) -> bool:
    lowered = argument.lower()
    if lowered in {"yes", "y", "true", "t", "1", "enable", "on"}:
        return True
    elif lowered in {"no", "n", "false", "f", "0", "disable", "off"}:
        return False
    else:
        raise BadArgument(f"{argument} is not a recognised boolean option.")


def _is_nested(func: Callable) -> bool:
    # we need to ignore parameters like self and ctx, so this is the easiest way
    # forgive me, but this is the only reliable way i can find out if the function
    # is in a class
    # as the name of this suggests, it really only checks if it's nested in
    # a function, class, method, etc.
    # this method isn't perfect at all, but it's the best way without hooking into
    # dis-snek itself.
    return "." in func.__qualname__


def _merge_converters(converter_dict: dict[type, type[Converter]]) -> dict[type, type[Converter]]:
    return SNEK_OBJECT_TO_CONVERTER | converter_dict


def _get_from_anno_type(anno: Annotated, name: str) -> Any:
    """
    Handles dealing with Annotated annotations, getting their \
    (first and what should be only) type annotation.

    This allows correct type hinting with, say, Converters, for example.
    """
    # this is treated how it usually is during runtime
    # the first argument is ignored and the rest is treated as is

    args = typing.get_args(anno)[1:]
    if len(args) > 1:
        # we could treat this as a union, but id rather have a user
        # use an actual union type here
        # from what ive seen, multiple arguments for Annotated are
        # meant to be used to narrow down a type rather than
        # be used as a union anyways
        raise ValueError(f"{_get_name(anno)} for {name} has more than 2 arguments, which is unsupported.")

    return args[0]


def _get_converter_function(anno: type[Converter] | Converter, name: str) -> Callable[[PrefixedContext, str], Any]:
    num_params = len(inspect.signature(anno.convert).parameters.values())

    # if we have three parameters for the function, it's likely it has a self parameter
    # so we need to get rid of it by initing - typehinting hates this, btw!
    # the below line will error out if we aren't supposed to init it, so that works out
    actual_anno: Converter = anno() if num_params == 3 else anno  # type: ignore
    # we can only get to this point while having three params if we successfully inited
    if num_params == 3:
        num_params -= 1

    if num_params != 2:
        ValueError(f"{_get_name(anno)} for {name} is invalid: converters must have exactly 2 arguments.")

    return actual_anno.convert


def _get_converter(anno: type, name: str, type_to_converter: dict[type, type[Converter]]) -> Callable[[PrefixedContext, str], Any]:  # type: ignore
    if typing.get_origin(anno) == Annotated:
        anno = _get_from_anno_type(anno, name)

    if isinstance(anno, Converter):
        return _get_converter_function(anno, name)
    elif converter := type_to_converter.get(anno, None):
        return _get_converter_function(converter, name)
    elif typing.get_origin(anno) is Literal:
        literals = typing.get_args(anno)
        return LiteralConverter(literals).convert
    elif inspect.isfunction(anno):
        num_params = len(inspect.signature(anno).parameters.values())
        match num_params:
            case 2:
                return lambda ctx, arg: anno(ctx, arg)
            case 1:
                return lambda ctx, arg: anno(arg)
            case 0:
                return lambda ctx, arg: anno()
            case _:
                ValueError(f"{_get_name(anno)} for {name} has more than 2 arguments, which is unsupported.")
    elif anno == bool:
        return lambda ctx, arg: _convert_to_bool(arg)
    elif anno == inspect._empty:
        return lambda ctx, arg: str(arg)
    else:
        return lambda ctx, arg: anno(arg)


def _greedy_parse(greedy: Greedy, param: inspect.Parameter) -> Any:
    if param.kind in {param.KEYWORD_ONLY, param.VAR_POSITIONAL}:
        raise ValueError("Greedy[...] cannot be a variable or keyword-only argument.")

    arg = typing.get_args(greedy)[0]

    if typing.get_origin(arg) == Annotated:
        arg = _get_from_anno_type(arg, param.name)

    if arg in {NoneType, str}:
        raise ValueError(f"Greedy[{_get_name(arg)}] is invalid.")

    if typing.get_origin(arg) in {Union, UnionType} and NoneType in typing.get_args(arg):
        raise ValueError(f"Greedy[{repr(arg)}] is invalid.")

    return arg


def _get_params(
    func: Callable, has_self: bool, type_to_converter: dict[type, type[Converter]]
) -> list[CommandParameter]:
    cmd_params: list[CommandParameter] = []

    # ignoring self it is exists
    if has_self:
        callback = functools.partial(func, None, None)
    else:
        callback = functools.partial(func, None)

    params = inspect.signature(callback).parameters
    for name, param in params.items():
        cmd_param = CommandParameter()
        cmd_param.name = name
        cmd_param.default = param.default if param.default is not param.empty else MISSING

        cmd_param.type = anno = param.annotation

        if typing.get_origin(anno) == Greedy:
            anno = _greedy_parse(anno, param)
            cmd_param.greedy = True

        if typing.get_origin(anno) in {Union, UnionType}:
            cmd_param.union = True
            for arg in typing.get_args(anno):
                if arg != NoneType:
                    converter = _get_converter(arg, name, type_to_converter)
                    cmd_param.converters.append(converter)
                elif not cmd_param.optional:  # d.py-like behavior
                    cmd_param.default = None
        else:
            converter = _get_converter(anno, name, type_to_converter)
            cmd_param.converters.append(converter)

        match param.kind:
            case param.KEYWORD_ONLY:
                cmd_param.consume_rest = True
                cmd_params.append(cmd_param)
                break
            case param.VAR_POSITIONAL:
                if cmd_param.optional:
                    # there's a lot of parser ambiguities here, so i'd rather not
                    raise ValueError("Variable arguments cannot have default values or be Optional.")

                cmd_param.variable = True
                cmd_params.append(cmd_param)
                break

        cmd_params.append(cmd_param)

    return cmd_params


def _arg_fix(arg: str) -> str:
    return arg[1:-1] if arg[0] in _quotes.keys() else arg


async def maybe_coroutine(func: Callable, *args, **kwargs) -> Any:
    """Allows running either a coroutine or a function."""
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    else:
        return func(*args, **kwargs)


async def _convert(param: CommandParameter, ctx: PrefixedContext, arg: str) -> tuple[Any, bool]:
    converted = MISSING
    for converter in param.converters:
        try:
            converted = await maybe_coroutine(converter, ctx, arg)
            break
        except Exception as e:
            if not param.union and not param.optional:
                if isinstance(e, BadArgument):
                    raise
                raise BadArgument(str(e)) from e

    used_default = False
    if converted == MISSING:
        if param.optional:
            converted = param.default
            used_default = True
        else:
            union_types = typing.get_args(param.type)
            union_names = tuple(_get_name(t) for t in union_types)
            union_types_str = ", ".join(union_names[:-1]) + f", or {union_names[-1]}"
            raise BadArgument(f'Could not convert "{arg}" into {union_types_str}.')

    return converted, used_default


async def _greedy_convert(
    param: CommandParameter, ctx: PrefixedContext, args: ArgsIterator
) -> tuple[list[Any] | Any, bool]:
    args.back()
    broke_off = False
    greedy_args = []

    for arg in args:
        try:
            greedy_arg, used_default = await _convert(param, ctx, arg)

            if used_default:
                raise BadArgument

            greedy_args.append(greedy_arg)
        except BadArgument:
            broke_off = True
            break

    if not greedy_args:
        if param.default:
            greedy_args = param.default  # im sorry, typehinters
        else:
            raise BadArgument(f"Failed to find any arguments for {repr(param.type)}.")

    return greedy_args, broke_off


@define()
class MolterCommand(PrefixedCommand):
    parameters: list[CommandParameter] = field(metadata=docs("The paramters of the command."), factory=list)
    aliases: list[str] = field(
        metadata=docs(
            "The list of aliases the command can be invoked under. Requires one of the override classes to work."
        ),
        factory=list,
    )
    hidden: bool = field(
        metadata=docs("If `True`, the default help command does not show this in the help output."), default=False
    )
    ignore_extra: bool = field(
        metadata=docs(
            "If `True`, ignores extraneous strings passed to a command if all its requirements are met (e.g. ?foo a b c"
            " when only expecting a and b). Otherwise, an error is raised. Defaults to True."
        ),
        default=True,
    )
    hierarchical_checking: bool = field(
        metadata=docs(
            "If `True` and if the base of a subcommand, every subcommand underneath it will run this command's checks"
            " before its own. Otherwise, only the subcommand's checks are checked."
        ),
        default=True,
    )
    help: Optional[str] = field(metadata=docs("The long help text for the command."), default=None)
    brief: Optional[str] = field(metadata=docs("The short help text for the command."), default=None)
    parent: Optional["MolterCommand"] = field(metadata=docs("The parent command, if applicable."), default=None)
    command_dict: dict[str, "MolterCommand"] = field(
        metadata=docs("A dict of a subcommand's name and the subcommand for this command."), factory=dict
    )
    _usage: Optional[str] = field(default=None)
    _type_to_converter: dict[type, type[Converter]] = field(
        default=SNEK_OBJECT_TO_CONVERTER, converter=_merge_converters
    )

    def __attrs_post_init__(self) -> None:
        super().__attrs_post_init__()  # we want checks to work

        # we have to do this afterwards as these rely on the callback
        # and its own value, which is impossible to get with attrs
        # methods, i think

        if self.help:
            self.help = inspect.cleandoc(self.help)
        else:
            self.help = inspect.getdoc(self.callback)
            if isinstance(self.help, bytes):
                self.help = self.help.decode("utf-8")

        if self.brief is None:
            self.brief = self.help.splitlines()[0] if self.help is not None else None

    def __hash__(self) -> int:
        return id(self)

    @property
    def usage(self) -> str:
        """
        A string displaying how the command can be used.

        If no string is set, it will default to the command's signature.
        Useful for help commands.
        """
        return self._usage or self.signature

    @usage.setter
    def usage(self, usage: str) -> None:
        self._usage = usage

    @property
    def qualified_name(self) -> str:
        """Returns the full qualified name of this command."""
        name_deq = deque()
        command = self

        while command.parent is not None:
            name_deq.appendleft(command.name)
            command = command.parent

        name_deq.appendleft(command.name)
        return " ".join(name_deq)

    @property
    def all_commands(self) -> frozenset["MolterCommand"]:
        """Returns all unique subcommands underneath this command."""
        return frozenset(self.command_dict.values())

    @property
    def signature(self) -> str:
        """Returns a POSIX-like signature useful for help command output."""
        if not self.parameters:
            return ""

        results = []

        for param in self.parameters:
            anno = param.type
            name = param.name

            if typing.get_origin(anno) == Annotated:
                # message commands can only have two arguments in an annotation anyways
                anno = typing.get_args(anno)[1]

            if not param.greedy and param.union:
                union_args = typing.get_args(anno)
                if len(union_args) == 2 and param.optional:
                    anno = union_args[0]

            if typing.get_origin(anno) is Literal:
                # it's better to list the values it can be than display the variable name itself
                name = "|".join(f'"{v}"' if isinstance(v, str) else str(v) for v in typing.get_args(anno))

            # we need to do a lot of manipulations with the signature
            # string, so using a list as a string builder makes sense for performance
            result_builder: list[str] = []

            if param.optional and param.default is not None:
                # it would be weird making it look like name=None
                result_builder.append(f"{name}={param.default}")
            else:
                result_builder.append(name)

            if param.variable:
                # this is inside the brackets
                result_builder.append("...")

            # surround the result with brackets
            if param.optional:
                result_builder.insert(0, "[")
                result_builder.append("]")
            else:
                result_builder.insert(0, "<")
                result_builder.append(">")

            if param.greedy:
                # this is outside the brackets, making it differentiable from
                # a variable argument
                result_builder.append("...")

            results.append("".join(result_builder))

        return " ".join(results)

    def parse_parameters(self, *, has_self: bool) -> None:
        """
        Parses the parameters that this command has into a form Molter can use.

        These are automatically ran with the decorators, though this needs to be run manually if
        you wish to create a `MolterCommand` object manually.
        Command arguments will not be used otherwise.

        Args:
            has_self (`bool`): If this command has a `self` parameter or not. If so, Molter will
            make sure to ignore it while parsing.
        """
        self.parameters = _get_params(self.callback, has_self, self._type_to_converter)

    def add_command(self, cmd: "MolterCommand") -> None:
        """Adds a command as a subcommand to this command."""
        cmd.parent = self  # just so we know this is a subcommand

        cmd_names = frozenset(self.command_dict)
        if cmd.name in cmd_names:
            raise ValueError(
                f"Duplicate Command! Multiple commands share the name/alias `{self.qualified_name} {cmd.name}`"
            )
        self.command_dict[cmd.name] = cmd

        for alias in cmd.aliases:
            if alias in cmd_names:
                raise ValueError(
                    f"Duplicate Command! Multiple commands share the name/alias `{self.qualified_name} {cmd.name}`"
                )
            self.command_dict[alias] = cmd

    def remove_command(self, name: str) -> None:
        """
        Removes a command as a subcommand from this command.

        If an alias is specified, only the alias will be removed.
        """
        command = self.command_dict.pop(name, None)

        if command is None or name in command.aliases:
            return

        for alias in command.aliases:
            self.command_dict.pop(alias, None)

    def get_command(self, name: str) -> Optional["MolterCommand"]:
        """
        Gets a subcommand from this command. Can get subcommands of subcommands if needed.

        Args:
            name (`str`): The command to search for.

        Returns:
            `MolterCommand`: The command object, if found.
        """
        if " " not in name:
            return self.command_dict.get(name)

        names = name.split()
        if not names:
            return None

        cmd = self.command_dict.get(names[0])
        if not cmd or not cmd.command_dict:
            return cmd

        for name in names[1:]:
            try:
                cmd = cmd.command_dict[name]
            except (AttributeError, KeyError):
                return None

        return cmd

    def subcommand(
        self,
        name: Optional[str] = None,
        *,
        aliases: Optional[list[str]] = None,
        help: Optional[str] = None,
        brief: Optional[str] = None,
        usage: Optional[str] = None,
        enabled: bool = True,
        hidden: bool = False,
        ignore_extra: bool = True,
        hierarchical_checking: bool = True,
        type_to_converter: Optional[dict[type, type[Converter]]] = None,
    ) -> (Callable[..., "MolterCommand"]):
        """
        A decorator to declare a subcommand for a Molter message command.

        Parameters:
            name (`str`, optional): The name of the command.
            Defaults to the name of the coroutine.

            aliases (`list[str]`, optional): The list of aliases the
            command can be invoked under.
            Requires one of the override classes to work.

            help (`str`, optional): The long help text for the command.
            Defaults to the docstring of the coroutine, if there is one.

            brief (`str`, optional): The short help text for the command.
            Defaults to the first line of the help text, if there is one.

            usage(`str`, optional): A string displaying how the command
            can be used. If no string is set, it will default to the
            command's signature. Useful for help commands.

            enabled (`bool`, optional): Whether this command can be run
            at all. Defaults to True.

            hidden (`bool`, optional): If `True`, the default help
            command (when it is added) does not show this in the help
            output. Defaults to False.

            ignore_extra (`bool`, optional): If `True`, ignores extraneous
            strings passed to a command if all its requirements are met
            (e.g. ?foo a b c when only expecting a and b).
            Otherwise, an error is raised. Defaults to True.

            hierarchical_checking (`bool`, optional): If `True` and if the
            base of a subcommand, every subcommand underneath it will run
            this command's checks before its own. Otherwise, only the
            subcommand's checks are checked. Defaults to True.

            type_to_converter (`dict[type, type[Converter]]`, optional): A dict
            that associates converters for types. This allows you to use
            native type annotations without needing to use `typing.Annotated`.
            If this is not set, only dis-snek classes will be converted using
            built-in converters.

        Returns:
            `molter.MolterCommand`: The command object.
        """

        def wrapper(func: Callable) -> "MolterCommand":
            cmd = MolterCommand(  # type: ignore
                callback=func,
                name=name or func.__name__,
                aliases=aliases or [],
                help=help,
                brief=brief,
                usage=usage,  # type: ignore
                enabled=enabled,
                hidden=hidden,
                ignore_extra=ignore_extra,
                hierarchical_checking=hierarchical_checking,
                type_to_converter=type_to_converter or getattr(func, "_type_to_converter", {}),  # type: ignore
            )
            cmd.parse_parameters(has_self=_is_nested(func))
            self.add_command(cmd)
            return cmd

        return wrapper

    async def call_callback(self, callback: Callable, ctx: PrefixedContext) -> None:
        """
        Runs the callback of this command.

        Args:
            callback (`Callable`): The callback to run. This is provided for compatibility with dis_snek.
            ctx (`dis_snek.PrefixedContext`): The context to use for this command.
        """
        # sourcery skip: remove-empty-nested-block, remove-redundant-if, remove-unnecessary-else
        if len(self.parameters) == 0:
            return await callback(ctx)
        else:
            # this is slightly costly, but probably worth it
            ctx.args = ARGS_PARSE.findall(ctx.content_parameters)

            new_args: list[Any] = []
            kwargs: dict[str, Any] = {}
            args = ArgsIterator(tuple(_arg_fix(a) for a in ctx.args))
            param_index = 0

            for arg in args:
                while param_index < len(self.parameters):
                    param = self.parameters[param_index]

                    if param.consume_rest:
                        arg = " ".join(args.consume_rest())

                    if param.variable:
                        args_to_convert = args.consume_rest()
                        new_arg = [await _convert(param, ctx, arg) for arg in args_to_convert]
                        new_arg = tuple(arg[0] for arg in new_arg)
                        new_args.append(new_arg)
                        param_index += 1
                        break

                    if param.greedy:
                        greedy_args, broke_off = await _greedy_convert(param, ctx, args)

                        new_args.append(greedy_args)
                        param_index += 1
                        if broke_off:
                            args.back()

                        if param.default:
                            continue
                        else:
                            break

                    converted, used_default = await _convert(param, ctx, arg)
                    if not param.consume_rest:
                        new_args.append(converted)
                    else:
                        kwargs[param.name] = converted
                    param_index += 1

                    if not used_default:
                        break

            if param_index < len(self.parameters):
                for param in self.parameters[param_index:]:
                    if not param.optional:
                        raise BadArgument(f"{param.name} is a required argument that is missing.")
                    else:
                        if not param.consume_rest:
                            new_args.append(param.default)
                        else:
                            kwargs[param.name] = param.default
                            break
            elif not self.ignore_extra and not args.finished:
                raise BadArgument(f"Too many arguments passed to {self.name}.")

            return await callback(ctx, *new_args, **kwargs)


def message_command(
    name: Optional[str] = None,
    *,
    aliases: Optional[list[str]] = None,
    help: Optional[str] = None,
    brief: Optional[str] = None,
    usage: Optional[str] = None,
    enabled: bool = True,
    hidden: bool = False,
    ignore_extra: bool = True,
    hierarchical_checking: bool = True,
    type_to_converter: Optional[dict[type, type[Converter]]] = None,
) -> Callable[..., MolterCommand]:
    """
    A decorator to declare a coroutine as a Molter message command.

    Parameters:
        name (`str`, optional): The name of the command.
        Defaults to the name of the coroutine.

        aliases (`list[str]`, optional): The list of aliases the
        command can be invoked under.
        Requires one of the override classes to work.

        help (`str`, optional): The long help text for the command.
        Defaults to the docstring of the coroutine, if there is one.

        brief (`str`, optional): The short help text for the command.
        Defaults to the first line of the help text, if there is one.

        usage(`str`, optional): A string displaying how the command
        can be used. If no string is set, it will default to the
        command's signature. Useful for help commands.

        enabled (`bool`, optional): Whether this command can be run
        at all. Defaults to True.

        hidden (`bool`, optional): If `True`, the default help
        command (when it is added) does not show this in the help
        output. Defaults to False.

        ignore_extra (`bool`, optional): If `True`, ignores extraneous
        strings passed to a command if all its requirements are met
        (e.g. ?foo a b c when only expecting a and b).
        Otherwise, an error is raised. Defaults to True.

        hierarchical_checking (`bool`, optional): If `True` and if the
        base of a subcommand, every subcommand underneath it will run
        this command's checks before its own. Otherwise, only the
        subcommand's checks are checked. Defaults to True.

        type_to_converter (`dict[type, type[Converter]]`, optional): A dict
        that associates converters for types. This allows you to use
        native type annotations without needing to use `typing.Annotated`.
        If this is not set, only dis-snek classes will be converted using
        built-in converters.

    Returns:
        `molter.MolterCommand`: The command object.
    """

    def wrapper(func: Callable) -> MolterCommand:
        cmd = MolterCommand(  # type: ignore
            callback=func,
            name=name or func.__name__,
            aliases=aliases or [],
            help=help,
            brief=brief,
            usage=usage,  # type: ignore
            enabled=enabled,
            hidden=hidden,
            ignore_extra=ignore_extra,
            hierarchical_checking=hierarchical_checking,
            type_to_converter=type_to_converter or getattr(func, "_type_to_converter", {}),  # type: ignore
        )
        cmd.parse_parameters(has_self=_is_nested(func))
        return cmd

    return wrapper


msg_command = message_command

# molter command typevar - can be the function or the command
MCT = TypeVar("MCT", Callable, MolterCommand)


def register_converter(anno_type: type, converter: type[Converter]) -> Callable[..., MCT]:
    """
    A decorator that allows you to register converters for a type for a specific command.

    This allows for native type annotations without needing to use `typing.Annotated`.

    Args:
        anno_type (`type`): The type to register for.
        converter (`type[Converter]`): The converter to use for the type.

    Returns:
        `Callable | MolterCommand`: Either the callback or the command.
        If this is used after using the `molter.message_command` decorator, it will be a command.
        Otherwise, it will be a callback.
    """

    def wrapper(command: MCT) -> MCT:
        if hasattr(command, "_type_to_converter"):
            command._type_to_converter[anno_type] = converter
        else:
            command._type_to_converter = {anno_type: converter}

        if isinstance(command, MolterCommand):
            # we want to update any instance where the anno_type was used
            # to use the provided converter without re-analyzing every param
            for param in command.parameters:
                param_type = param.type
                if anno_type == param_type:
                    param.converters = [converter]
                else:
                    if typing.get_origin(param_type) == Annotated:
                        param_type = _get_from_anno_type(param_type, param.name)

                    with suppress(ValueError):
                        # if you have multiple of the same anno/type here, i don't know
                        # what to tell you other than why
                        index = typing.get_args(param.type).index(anno_type)
                        param.converters[index] = converter

        return command

    return wrapper
