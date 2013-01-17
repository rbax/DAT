"""Interface with VisTrails packages.

This is the only module that VisTrails packages need to import. It provides
the classes and methods necessary to define plot types and variable loaders.

You might want to maintain compatibility with VisTrails, like so:
try:
    import dat.vistrails_interface
    from dat.gui import translate # Optional; you might want to use it if you
        # want to internationalize your strings
except ImportError:
    pass # This happens if the package was imported from VisTrails, not from
        # DAT
        # In that case, don't define plots or variable loaders.
else:
    _ = translate('packages.MyPackage') # Create a translator (optional)

    _plots = [
        Plot(...),
    ]

    class MyLoader(dat.vistrails_interface.CustomVariableLoader):
        ...

    _variable_loaders = [
        MyLoader: _("My new loader"),
    ]
"""

import importlib
import inspect
import os, os.path
from PyQt4 import QtGui

from dat import DEFAULT_VARIABLE_NAME
import dat.manager
from dat.plot_map import PipelineInformation

from vistrails.core import get_vistrails_application
from vistrails.core.db.action import create_action
from vistrails.core.db.locator import XMLFileLocator
from vistrails.core.modules.module_registry import get_module_registry
from vistrails.core.modules.utils import parse_descriptor_string
from vistrails.core.modules.vistrails_module import Module
from vistrails.packages.spreadsheet.spreadsheet_execute import \
    executePipelineWithProgress


class Plot(object):
    def __init__(self, name, **kwargs):
        """A plot descriptor.

        Describes a Plot. These objects should be created by a VisTrails
        package for each Plot it want to registers with DAT, and added to a
        global '_plots' variable in the 'init' module (for a reloadable
        package).

        name is mandatory and will be displayed to the user.
        description is a text that explains what your Plot is about, and can be
        localized.
        ports should be a list of Port objects describing the input your Plot
        expects.
        subworkflow is the path to the subworkflow that will be used for this
        Plot. In this string, '{package_dir}' will be replaced with the current
        package's path.
        """
        self.name = name
        self.description = kwargs.get('description')

        caller = inspect.currentframe().f_back
        package = os.path.dirname(inspect.getabsfile(caller))

        # Build plot from a subworkflow
        self.subworkflow = kwargs['subworkflow'].format(package_dir=package)
        self.ports = kwargs['ports']


class Port(object):
    def __init__(self, name, type=Module, optional=False):
        self.name = name
        self.type = type
        self.optional = optional


class ModuleWrapper(object):
    """Object representing a VisTrails module in a DAT variable pipeline.

    This is a wrapper returned by Variable#add_module. It is used by VisTrails
    packages to build a pipeline for a new variable.
    """
    def __init__(self, variable, module_type):
        self._variable = variable
        reg = get_module_registry()
        if isinstance(module_type, str):
            try:
                pkg = importlib.import_module(self._variable._vt_package)
                identifier = pkg.identifier
            except ImportError, AttributeError:
                identifier = None
            d_tuple = parse_descriptor_string(module_type, identifier)
            descriptor = reg.get_descriptor_by_name(*d_tuple)
        elif issubclass(module_type, Module):
            descriptor = reg.get_descriptor(module_type)
        else:
            raise TypeError("add_module() argument must be a Module or str "
                            "object, not '%s'" % type(module_type))
        controller = self._variable._controller
        self._module = controller.create_module_from_descriptor(descriptor)
        self._variable._operations.append(('add', self._module))

    def add_function(self, inputport_name, vt_type, value):
        """Add a function for a port of this module.
        """
        # TODO-dat : Check type and port name
        controller = self._variable._controller
        self._variable._operations.extend(
                controller.update_function_ops(
                        self._module,
                        inputport_name,
                        [value]))

    def connect_outputport_to(self, outputport_name, other_module, inputport_name):
        """Create a connection between ports of two modules.

        Connects the given output port of this module to the given input port
        of another module.

        The modules must be wrappers for the same Variable.
        """
        if self._variable is not other_module._variable:
            raise ValueError("connect_outputport_to() can only connect "
                             "modules of the same Variable")
        # Might raise vistrails.core.modules.module_registry:MissingPort
        controller = self._variable._controller
        connection = controller.create_connection(
                self._module, outputport_name,
                other_module, inputport_name)
        self._variable._operations.append(('add', connection))


class Variable(object):
    """Object representing a DAT variable.

    This is a wrapper used by VisTrails packages to build a pipeline for a new
    variable. This variable is then stored in the Manager.
    Wrapper objects are restored from the Vistrail file easily: they are
    children versions of the version tagged 'dat-vars', and have a tag
    'dat-var-name' where 'name' is the name of that specific DAT variable.
    """
    @staticmethod
    def _get_variables_root():
        """Create or get the version tagged 'dat-vars'

        This is the base version of all DAT variables. It consists of a single
        OutputPort module with name 'value'.
        """
        controller = get_vistrails_application().dat_controller
        if controller.vistrail.has_tag_str('dat-vars'):
            root_version = controller.vistrail.get_version_number('dat-vars')
        else:
            # Create the 'dat-vars' version
            controller.change_selected_version(0)
            controller.add_module_action
            reg = get_module_registry()
            operations = []

            # Add an OutputPort module
            descriptor = reg.get_descriptor_by_name(
                    'edu.utah.sci.vistrails.basic', 'OutputPort')
            out_mod = controller.create_module_from_descriptor(descriptor)
            operations.append(('add', out_mod))

            # Add a function to this module
            operations.extend(
                    controller.update_function_ops(
                            out_mod,
                            'name',
                            ['value']))

            # Perform the operations
            action = create_action(operations)
            controller.add_new_action(action)
            root_version = controller.perform_action(action)
            controller.change_selected_version(root_version)
            # Tag as 'dat-vars'
            controller.vistrail.set_tag(root_version, 'dat-vars')

        pipeline = controller.vistrail.getPipeline(root_version)
        outmod_id = pipeline.modules.keys()
        assert len(outmod_id) == 1
        outmod_id = outmod_id[0]
        return controller, root_version, outmod_id

    def __init__(self, type=None):
        self.type = type
        # Create or get the version tagged 'dat-vars'
        self._controller, self._root_version, self._output_module_id = (
                Variable._get_variables_root())

        # The creation of a Variable is bufferized so as to handle exceptions
        # in VisTrails packages correctly
        # All the operations leading to the materialization of this variable
        # as a pipeline, child of the 'dat-vars' version, are stored in this
        # list and will be added to the Vistrail when perform_operations() is
        # called by the Manager
        self._operations = []

        # Get the VisTrails package that's creating this Variable by inspecting
        # the stack
        caller = inspect.currentframe().f_back
        module = inspect.getmodule(caller).__name__
        if module.endswith('.__init__'):
            module = module[:-9]
        if module.endswith('.init'):
            module = module[:-5]
        self._vt_package = module

        self._output_designated = False

    def add_module(self, module_type):
        # Add a new module to the pipeline and return a wrapper
        return ModuleWrapper(self, module_type)

    def select_output_port(self, module, outputport_name):
        """Select the output port of the Variable pipeline.

        The given output port of the given module will be chosen as the output
        port of the Variable. It is this output port that will be connected to
        the Plot subworkflow's input port when creating an actual pipeline.

        This function should be called exactly once when creating a Variable.
        """
        # Connects the output port with the given name of the given wrapped
        # module to the OutputPort module (added at version 'dat-vars')
        # TODO-dat : Check that the port is compatible to self.type
        if module._variable is not self:
            raise ValueError("select_output_port() designated a module from a "
                             "different Variable")
        elif self._output_designated:
            raise ValueError("select_output_port() was called more than once")

        controller = self._controller
        out_mod = controller.current_pipeline.modules[self._output_module_id]
        connection = controller.create_connection(
                module._module, outputport_name,
                out_mod, 'InternalPipe')
        self._operations.append(('add', connection))
        self._output_designated = True

    def perform_operations(self):
        """Materialize this Variable in the Vistrail.

        Create a pipeline tagged as 'dat-var-<varname>' for this Variable,
        children of the 'dat-vars' version.

        This is called by the Manager when the Variable is inserted.
        """
        controller = self._controller
        controller.change_selected_version(self._root_version)

        action = create_action(self._operations)
        controller.add_new_action(action)
        self._var_version = controller.perform_action(action)
        controller.vistrail.set_tag(self._var_version,
                                    'dat-var-%s' % self.name)
        controller.change_selected_version(self._var_version)

    def remove(self):
        """Delete the pipeline from the Vistrail.

        This is called by the Manager when the Variable is removed.
        """
        controller = self._controller
        controller.prune_versions([self._var_version])

    def rename(self):
        """Change the tag on this version in the Vistrail.

        This is called by the Manager when the Variable is renamed.
        """
        controller = self._controller
        controller.vistrail.set_tag(self._var_version,
                                    'dat-var-%s' % self.name)

    def _get_name(self):
        return dat.manager.Manager()._get_variable_name(self)
    name = property(_get_name)


class _BaseVariableLoader(object):
    def __init__(self):
        self.default_variable_name_observer = None

    def reset(self):
        """Resets the widget so it can be used again.

        Implement this in subclasses to reset the widget.
        """
        pass

    def get_default_variable_name(self):
        """Default name for the variable that will be loaded.

        You should re-implement this to return a sensible default name for the
        variable that will be loaded. The user can edit it if need be.
        You don't need to worry about already taken names, this default will be
        made unique if need be.
        """
        return DEFAULT_VARIABLE_NAME

    def default_variable_name_changed(self, new_default_name):
        """Call this function to signal that the default variable name changed.

        This can happen if the user selected a different file, ...
        """
        if self.default_variable_name_observer is not None:
            self.default_variable_name_observer(self, new_default_name)


class CustomVariableLoader(QtGui.QWidget, _BaseVariableLoader):
    """Custom variable loading tab.

    These loaders show up in a tab of their own, allowing to load any kind of
    data from any source.

    It is a widget that the user will use to choose the data he wants to load.
    load() will be called when the user confirms to actually create a Variable
    object.
    reset() is called to reset the widget to its original settings, so that it
    can be reused to load something else.
    get_default_variable_name() should return a sensible variable name for the
    variable that will be loaded; the user can edit it if need be.
    If the default variable name changes because of the user changing its
    selection, default_variable_name_changed() can be called to update it.
    """
    def __init__(self):
        QtGui.QWidget.__init__(self)
        _BaseVariableLoader.__init__(self)

    def load(self):
        """Load the variable and return it.

        Implement this in subclasses to load whatever data the user selected as
        a Variable object.
        """
        raise NotImplementedError


class FileVariableLoader(QtGui.QWidget, _BaseVariableLoader):
    """A loader that gets a variable from a file.

    Subclasses do not get a tab of their own, but appear on the "File" tab if
    they indicate they are able to load the selected file.
    """
    @classmethod
    def can_load(cls, filename):
        """Indicates whether this loader can read the given file.

        If true, it will be selectable by the user.
        You have to implement this in subclasses.

        Do not actually load the data here, you should only do quick checks
        (like file extension or magic number).
        """
        return False

    def __init__(self):
        """Constructor.

        This constructor receives a 'filename' parameter: the file that we want
        to load. Do not keep the file open thoughout the life of this object,
        it could interfere with other loaders.
        """
        QtGui.QWidget.__init__(self)
        _BaseVariableLoader.__init__(self)

    def load(self):
        """Load the variable and return it.

        Implement this in subclasses to do the actual loading of the variable
        from the filename that was given to the constructor, using the desired
        parameters.
        """
        raise NotImplementedError


def _get_function(module, function_name):
    """Get the value of a function of a pipeline module.
    """
    for function in module.functions:
        if function.name == function_name:
            if len(function.params) > 0:
                return function.params[0].strValue
    return None


def create_pipeline(recipe):
    """create_pipeline(recipe: DATRecipe) -> PipelineInformation
    
    Create a pipeline in the Vistrail and return its information.
    """
    # Build from the root version
    controller = get_vistrails_application().dat_controller
    controller.change_selected_version(0)

    reg = get_module_registry()

    operations = []

    outputport_desc = reg.get_descriptor_by_name(
            'edu.utah.sci.vistrails.basic', 'OutputPort')
    inputport_desc = reg.get_descriptor_by_name(
            'edu.utah.sci.vistrails.basic', 'InputPort')

    # Add the plot subworkflow module
    if False:
        # TODO-dat : create a subworkflow module for the Plot
        plot_module = None
    else:
        locator = XMLFileLocator(recipe.plot.subworkflow)
        vistrail = locator.load()
        version = vistrail.get_latest_version()
        plot_pipeline = vistrail.getPipeline(version)

        # Copy every module but the InputPorts
        new_modules_map = dict() # old module id -> new module
        for module in plot_pipeline.modules.itervalues():
            if module.module_descriptor is not inputport_desc:
                # We can't just add this module to the new pipeline!
                # We need to create a new one to avoid id collisions
                new_module = controller.create_module_from_descriptor(
                        module.module_descriptor)
                operations.append(('add', new_module))
                new_modules_map[module.id] = new_module
                # Copy the functions
                for function in module.functions:
                    operations.extend(
                            controller.update_function_ops(
                                    new_module,
                                    function.name,
                                    [param.strValue
                                     for param in function.params]))
                            # ModuleParameter has a 'name' attribute, but
                            # it doesn't seem to be useful

        # Copy the connections and locate the input ports
        plot_params = dict() # param name -> (module, input port name)
        for connection in plot_pipeline.connection_list:
            src = plot_pipeline.modules[connection.source.moduleId]
            if src.module_descriptor is inputport_desc:
                param = _get_function(src, 'name')
                plot_params[param] = (
                        new_modules_map[connection.destination.moduleId],
                        connection.destination.name)
            else:
                new_conn = controller.create_connection(
                        new_modules_map[connection.source.moduleId],
                        connection.source.name,
                        new_modules_map[connection.destination.moduleId],
                        connection.destination.name)
                operations.append(('add', new_conn))

    # Add the Variable subworkflows, but 'inline' them
    for param, variable in recipe.variables.iteritems():
        pipeline = controller.vistrail.getPipeline(
                'dat-var-%s' % variable.name)

        # Copy every module but the OutputPort
        copy_modules = dict()
        for module in pipeline.modules.itervalues():
            if (module.module_descriptor is outputport_desc and
                    _get_function(module, 'name') == 'value'):
                output_id = module.id
            else:
                operations.append(('add', module))
                copy_modules[module.id] = module

        # Copy every connection except the one to the OutputPort module
        for connection in pipeline.connection_list:
            if connection.destination.moduleId == output_id:
                if False:
                    # TODO-dat : use a subworkflow for the Plot
                    # We connect to the plot's subworkflow module port <param>
                    # instead
                    new_conn = controller.create_connection(
                            copy_modules[connection.source.moduleId],
                            connection.source.name,
                            plot_module,
                            param)
                    operations.append(('add', new_conn))
                else:
                    var_output_mod, var_output_port = plot_params[param]
                    new_conn = controller.create_connection(
                            copy_modules[connection.source.moduleId],
                            connection.source.name,
                            var_output_mod,
                            var_output_port)
                    operations.append(('add', new_conn))
            else:
                operations.append(('add', connection))

    action = create_action(operations)
    controller.add_new_action(action)
    pipeline_version = controller.perform_action(action)

    return PipelineInformation(pipeline_version)


def execute_pipeline_to_cell(cellInfo, pipeline):
    """ execute_pipeline_to_cell(cellInfo: CellInformation,
                             pipeline: PipelineInformation) -> None

    Execute the referenced pipeline, so that its result gets displayed in the
    specified spreadsheet cell.
    """
    # Retrieve the pipeline
    controller = get_vistrails_application().dat_controller
    pipeline = controller.vistrail.getPipeline(pipeline.version)
    pipeline = cellInfo.tab.setPipelineToLocateAt(
            cellInfo.row,
            cellInfo.column,
            pipeline)
    executePipelineWithProgress(
            pipeline,
            "DAT recipe execution")