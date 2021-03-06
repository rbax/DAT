import sys
import warnings

from dat import data_provenance
from dat.global_data import GlobalManager
from dat.operations import InvalidOperation, OperationWarning
from dat.operations.parsing import SYMBOL, NUMBER, STRING, OP, parse_expression
from dat.vistrail_data import VistrailManager
from dat import vistrails_interface
from dat.vistrails_interface import Variable, PipelineGenerator

from vistrails.core.modules.module_registry import get_module_registry
from vistrails.core.modules.vistrails_module import Module


class ComputeVariable(object):
    def execute(self, controller):
        raise NotImplementedError


class GetExistingVariable(ComputeVariable):
    def __init__(self, vistraildata, varname):
        self._variable = vistraildata.get_variable(varname)
        if self._variable is None:
            raise InvalidOperation("Unknown variable %r" % varname)
        self.type = self._variable.type

    def execute(self, controller):
        # Here we explicitely don't record that the Variable is already
        # materialized in the workflow, because we allow the user to copy
        # variables (i.e. enter an expression without any operation)
        return Variable.from_workflow(self._variable,
                                      record_materialized=False)


class BuildConstant(ComputeVariable):
    def __init__(self, value):
        self.value = value
        if isinstance(self.value, basestring):
            self.type = get_module_registry().get_descriptor_by_name(
                'org.vistrails.vistrails.basic',
                'String')
        else:  # isinstance(value, float):
            self.type = get_module_registry().get_descriptor_by_name(
                'org.vistrails.vistrails.basic',
                'Float')

    def execute(self, controller):
        generator = PipelineGenerator(controller)
        module = generator.controller.create_module_from_descriptor(self.type)
        generator.add_module(module)
        generator.update_function(module, 'value', [str(self.value)])
        return Variable(
            type=self.type,
            controller=controller,
            generator=generator,
            output=(module, 'value'),
            provenance=data_provenance.Constant(constant=self.value))


class ApplyOperation(ComputeVariable):
    def __init__(self, name, args):
        self._op = find_operation(name, [arg.type for arg in args])
        self.type = self._op.return_type
        self._args = args

    def execute(self, controller):
        """Recursively perform operations.
        """
        args = [arg.execute(controller) for arg in self._args]
        return apply_operation(controller, self._op, args)


def resolve_symbols(vistraildata, expr):
    if expr[0] == SYMBOL:
        # Get an existing variable
        return GetExistingVariable(vistraildata, expr[1])
    elif expr[0] == NUMBER or expr[0] == STRING:
        # Build a constant module
        return BuildConstant(expr[1])
    elif expr[0] == OP:
        # Find the right operation, comparing argument number and types
        name = expr[1]
        args = [resolve_symbols(vistraildata, arg) for arg in expr[2:]]
        if all(isinstance(arg, BuildConstant) for arg in args):
            if name == '+':
                return BuildConstant(args[0].value + args[1].value)
            elif name == '-':
                return BuildConstant(args[0].value - args[1].value)
            elif name == '*':
                return BuildConstant(args[0].value * args[1].value)
            elif name == '/':
                return BuildConstant(args[0].value / args[1].value)
        return ApplyOperation(name, args)


def perform_operation(expression, controller=None):
    """Perform a variable operation from the given string.
    """
    # First, parse the expressions
    target, expr_tree = parse_expression(expression)

    # Find the actual operations & variables
    controller, root_version, output_module_id = (
        Variable._get_variables_root(controller))
    vistraildata = VistrailManager(controller)
    if vistraildata.get_variable(target) is not None:
        raise InvalidOperation("Target variable %r already exists" % target)
    op_tree = resolve_symbols(vistraildata, expr_tree)

    # Build the new variable
    variable = op_tree.execute(controller)
    vistraildata.new_variable(target, variable)


def _fill_parent_modules_map(mod, parents, level):
    # Recursively walks the inheritance tree of 'mod', adding it to the map if
    # Module is a parent
    # Returns True if Module is a parent, so that the caller can add it to the
    # map and return True itself
    top_hit = False
    for parent in mod.__bases__:
        if parent == Module:
            parents[mod] = level
            return True
        else:
            if _fill_parent_modules_map(parent, parents, level + 1):
                parents[mod] = level
                top_hit = True
    return top_hit


def parent_modules(mod):
    """Get the parent Modules of a Module subclass.

    Returns a dict mapping each Module subclass to an int, that goes up from 0
    (for the given 'mod') to the class that directly inherits Module.
    """
    parents = dict()
    _fill_parent_modules_map(mod, parents, 0)
    return parents


def find_operation(name, args):
    """Choose the operation with the given name that accepts these arguments.
    """
    from dat.operations.builtins import builtin_operations

    # Initial list of considered operations: correct name
    operations = set([
        op
        for op in GlobalManager.variable_operations
        if op.name == name and op.usable_in_command])
    operations.update(builtin_operations.get(name, []))
    if not operations:
        raise InvalidOperation("There is no operation %r" % name)
    operations = set([
        op
        for op in operations
        if len(op.parameters) == len(args)])
    if not operations:
        raise InvalidOperation("There is no operation %r with %d arguments" % (
                               name, len(args)))

    # Loop on arguments
    for i, actual in enumerate(args):
        retained_operations = set()
        current_score = sys.maxint
        # All base classes
        bases = parent_modules(actual.module)
        for op in operations:
            for desc in op.parameters[i].types:
                expected = desc.module
                # Score of this operation for this argument
                try:
                    score = bases[expected]
                except KeyError:
                    pass  # This operation is not compatible with the argument
                else:
                    if score < current_score:
                        # Forget the previous operations, this one is better
                        # (i.e. the expected argument is more specific)
                        retained_operations = set([op])
                        current_score = score
                    elif score == current_score:
                        # This is as good as the other ones, add it to the list
                        # Their next argument will be examined
                        retained_operations.add(op)
                    # Else, not as good as the ones we have, discard

        operations = retained_operations
        if len(operations) == 0:
            break

    if len(operations) == 0:
        raise InvalidOperation("Found no match for operation %r with given "
                               "%d args" % (name, len(args)))
    if len(operations) > 1:
        warnings.warn(
            "Found several operations %r matching the given %d args" % (
                name, len(args)),
            category=OperationWarning)
    return next(iter(operations))


def apply_operation(controller, op, args):
    """Apply an operation to build a new variable.

    Either load the subworkflow or wrap the parameter variables correctly and
    call the callback function.
    """
    if op.callback is not None:
        # FIXME : controller is ignored here...
        assert controller == VistrailManager().controller
        result = vistrails_interface.call_operation_callback(
            op,
            op.callback,
            args)
        if result is None:
            raise InvalidOperation("Package error: operation callback "
                                   "returned None")
    else:  # op.subworkflow is not None:
        result = vistrails_interface.apply_operation_subworkflow(
            controller,
            op,
            op.subworkflow,
            args)

    if result.provenance is None:
        result.provenance = data_provenance.Operation(
            operation=op,
            arg_list=args)
    return result
