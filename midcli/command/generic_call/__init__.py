# -*- coding=utf-8 -*-
import copy
import logging

from middlewared.client import ClientException, ValidationErrors

from midcli.command.call_mixin import CallMixin
from midcli.command.common_syntax.argument import Argument, BooleanArgument, EnumArgument
from midcli.command.common_syntax.command import CommonSyntaxCommand
from midcli.command.interface import ProcessInputError
from midcli.utils.lang import undefined

logger = logging.getLogger(__name__)

__all__ = ["GenericCallCommand"]


class CallArgsError(ValueError):
    pass


class GenericCallCommand(CallMixin, CommonSyntaxCommand):
    def __init__(self, *args, method=None, splice_kwargs=None, **kwargs):
        self.method = self._process_method(copy.deepcopy(method))
        self.splice_kwargs = splice_kwargs

        self.arguments = []
        self._create_arguments()

        super().__init__(*args, **kwargs)

    def _process_method(self, method):
        """
        Transforms middleware method definition (e.g. rename poorly named arguments)
        """
        return method

    def _create_arguments(self):
        for i, item in enumerate(self.method["accepts"] or []):
            if i == self.splice_kwargs:
                if item["type"] != "object":
                    raise ValueError(f"For {self.method['name']!r} specified splice_kwargs={self.splice_kwargs} "
                                     f"for an item of type={item['type']!r}")

                for property_name in item["_attrs_order_"]:
                    self.arguments.append(self._create_argument(item["properties"][property_name]))
            else:
                self.arguments.append(self._create_argument(item))

    def _create_argument(self, item):
        name = item["_name_"]
        nullable = False

        type = item.get("type")
        if isinstance(type, list) and len(type) == 2 and "null" in type:
            nullable = True
            type = [x for x in type if x != "null"][0]

        default = item.get("default", undefined)

        args = (name, nullable, default)
        if type == "boolean":
            return BooleanArgument(*args)
        if "enum" in item:
            return EnumArgument(*args, enum=item["enum"])
        else:
            return Argument(*args)

    def _call_args(self, args, kwargs):
        args = args.copy()

        if self.splice_kwargs is not None:
            if len(args) < self.splice_kwargs:
                raise CallArgsError(f"Too few positional arguments ({self.splice_kwargs} required, "
                                    f"{len(args)} given)")

            if len(args) > self.splice_kwargs:
                raise CallArgsError(f"Too many positional arguments ({self.splice_kwargs} supported, "
                                    f"{len(args)} given)")

            args.append(kwargs)
        else:
            args_dict = dict(enumerate(args))
            for k, v in kwargs.items():
                for index, arg in enumerate(self.method["accepts"]):
                    if arg["_name_"] == k:
                        break
                else:
                    raise CallArgsError(f"Unknown keyword argument {k}")

                if index in args_dict:
                    raise CallArgsError(f"Keyword argument {k} already given as positional argument {index + 1}")

                args_dict[index] = v

            args = []
            if args_dict:
                for i in range(0, max(args_dict.keys()) + 1):
                    if i not in args_dict:
                        raise CallArgsError(f"Missing positional argument {i + 1} ({self.method['accepts'][i]['_name_']})")

                    args.append(args_dict[i])

        if self.method["accepts"]:
            for i, arg in enumerate(args):
                if i < len(self.method["accepts"]):
                    args[i] = self._call_arg(args[i], self.method["accepts"][i])

        return args

    def _call_arg(self, arg, schema):
        # Convert single values to lists for calls like `interface.create aliases="192.168.0.1"`
        if schema.get("type") == "array" and arg is not None and not isinstance(arg, list):
            return [arg]

        if schema.get("type") == "object" and schema.get("properties", {}) and isinstance(arg, dict):
            arg = arg.copy()
            for k in arg:
                if k in schema["properties"]:
                    arg[k] = self._call_arg(arg[k], schema["properties"][k])

        return arg

    def run(self, args, kwargs):
        if self.context.editor.is_available() and self._needs_editor(args, kwargs):
            self._run_with_editor(args, kwargs)
        else:
            self._run_with_args(args, kwargs)

    def _needs_editor(self, args, kwargs):
        return self.method["accepts"] and not (args or kwargs)

    def _run_with_editor(self, args, kwargs):
        self._run_editor([], [])

    def _run_editor(self, values, errors, method=None):
        schema = method or self.method
        while True:
            values = self.context.editor.edit(schema, values, errors)
            if values is None:
                return

            try:
                self.call(self.method["name"], *values, job=self.method["job"], raise_=True)
                return
            except ValidationErrors as e:
                errors = e.errors
                if self.context.editor.on_error(
                    title="Validation Errors",
                    text="\n".join([f"* {error.attribute}: {error.errmsg}" for error in errors]),
                ):
                    continue
                else:
                    return
            except ClientException as e:
                if self.context.editor.on_error("Error", e.error):
                    continue
                else:
                    return

    def _run_with_args(self, args, kwargs):
        try:
            call_args = self._call_args(args, kwargs)
        except CallArgsError as e:
            raise ProcessInputError(e.args[0])

        self.call(self.method["name"], *call_args, job=self.method["job"])
