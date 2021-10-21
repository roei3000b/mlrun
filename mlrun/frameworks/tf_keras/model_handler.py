import os
import shutil
import zipfile
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple, Union

import numpy as np
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import Model

import mlrun
from mlrun.artifacts import Artifact
from mlrun.features import Feature
from mlrun.frameworks._common import ModelHandler


# TODO: Implement a format handler for each model format and update the TFKerasModelHandler code accordingly.
class _ModelFormatHandler(ABC):
    """
    Handling files collection, saving and loading tf.keras model according to a specific format.
    """

    @staticmethod
    @abstractmethod
    def collect_files_from_store_object(*args, **kwargs) -> str:
        """
        Collect the needed model files from the given model object (store) to enable loading the model.
        """
        pass

    @staticmethod
    @abstractmethod
    def collect_files_from_local_path(*args, **kwargs) -> str:
        """
        Search for the needed model files from the given local path and collect them to enable loading the model.
        """
        pass

    @staticmethod
    @abstractmethod
    def save(*args, **kwargs):
        """
        Save the given model.
        """
        pass

    @staticmethod
    @abstractmethod
    def load(*args, **kwargs) -> keras.Model:
        """
        Load and return the keras model.
        """
        pass


class TFKerasModelHandler(ModelHandler):
    """
    Class for handling a tensorflow.keras model, enabling loading and saving it during runs.
    """

    # Declare a type of a tensor signature for ONNX conversion:
    TensorSignature = Union[tf.TensorSpec, np.ndarray]
    # Declare a type of an input sample:
    IOSample = Union[tf.Tensor, tf.TensorSpec, np.ndarray]

    class ModelFormats:
        """
        Model formats to pass to the 'TFKerasModelHandler' for loading and saving keras models.
        """

        SAVED_MODEL = "SavedModel"
        H5 = "H5"
        JSON_ARCHITECTURE_H5_WEIGHTS = "json_H5"

    def __init__(
        self,
        model_name: str,
        model_path: str = None,
        model: Model = None,
        context: mlrun.MLClientCtx = None,
        custom_objects_map: Union[Dict[str, Union[str, List[str]]], str] = None,
        custom_objects_directory: str = None,
        model_format: str = ModelFormats.SAVED_MODEL,
        save_traces: bool = False,
    ):
        """
        Initialize the handler. The model can be set here so it won't require loading. Notice that if the model path
        given is of a previously logged model (store model object path), all of the other configurations will be loaded
        automatically as they were logged with the model, hence they are optional.

        :param model_name:               The model name for saving and logging the model.
        :param model_path:               Path to the model's directory to load it from. The model files must start with
                                         the given model name and the directory must contain based on the given model
                                         formats:
                                         * SavedModel - A zip file 'model_name.zip' or a directory named 'model_name'.
                                         * H5 - A h5 file 'model_name.h5'.
                                         * Architecture and weights - The json file 'model_name.json' and h5 weight file
                                           'model_name.h5'.
                                         The model path can be also passed as a model object path in the following
                                         format: 'store://models/<PROJECT_NAME>/<MODEL_NAME>:<VERSION>'.
        :param model:                    Model to handle or None in case a loading parameters were supplied.
        :param context:                  MLRun context to work with for logging the model.
        :param custom_objects_map:       A dictionary of all the custom objects required for loading the model. Each key
                                         is a path to a python file and its value is the custom object name to import
                                         from it. If multiple objects needed to be imported from the same py file a list
                                         can be given. The map can be passed as a path to a json file as well. For
                                         example:
                                         {
                                             "/.../custom_optimizer.py": "optimizer",
                                             "/.../custom_layers.py": ["layer1", "layer2"]
                                         }
                                         All the paths will be accessed from the given 'custom_objects_directory',
                                         meaning each py file will be read from 'custom_objects_directory/<MAP VALUE>'.
                                         If the model path given is of a store object, the custom objects map will be
                                         read from the logged custom object map artifact of the model.
                                         Notice: The custom objects will be imported in the order they came in this
                                         dictionary (or json). If a custom object is depended on another, make sure to
                                         put it below the one it relies on.
        :param custom_objects_directory: Path to the directory with all the python files required for the custom
                                         objects. Can be passed as a zip file as well (will be extracted during the run
                                         before loading the model). If the model path given is of a store object, the
                                         custom objects files will be read from the logged custom object artifact of the
                                         model.
        :param model_format:             The format to use for saving and loading the model. Should be passed as a
                                         member of the class 'ModelFormats'. Defaulted to 'ModelFormats.SAVED_MODEL'.
        :param save_traces:              Whether or not to use functions saving (only available for the 'SavedModel'
                                         format) for loading the model later without the custom objects dictionary. Only
                                         from tensorflow version >= 2.4.0. Using this setting will increase the model
                                         saving size.

        :raise MLRunInvalidArgumentError: In case the input was incorrect:
                           * Model format is unrecognized.
                           * There was no model or model directory supplied.
                           * 'save_traces' parameter was miss-used.
        """
        # Validate given format:
        if model_format not in [
            TFKerasModelHandler.ModelFormats.SAVED_MODEL,
            TFKerasModelHandler.ModelFormats.H5,
            TFKerasModelHandler.ModelFormats.JSON_ARCHITECTURE_H5_WEIGHTS,
        ]:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "Unrecognized model format: '{}'. Please use one of the class members of "
                "'TFKerasModelHandler.ModelFormats'".format(model_format)
            )

        # Validate 'save_traces':
        if save_traces:
            if float(tf.__version__.rsplit(".", 1)[0]) < 2.4:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    "The 'save_traces' parameter can be true only for tensorflow versions >= 2.4. Current "
                    "version is {}".format(tf.__version__)
                )
            if model_format != TFKerasModelHandler.ModelFormats.SAVED_MODEL:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    "The 'save_traces' parameter is valid only for the 'SavedModel' format."
                )

        # Store the configuration:
        self._model_format = model_format
        self._save_traces = save_traces

        # If the model format is architecture and weights, this will hold the weights file collected:
        self._weights_file = None  # type: str

        # Setup the base handler class:
        super(TFKerasModelHandler, self).__init__(
            model_name=model_name,
            model_path=model_path,
            model=model,
            custom_objects_map=custom_objects_map,
            custom_objects_directory=custom_objects_directory,
            context=context,
        )

    def set_inputs(
        self,
        from_sample: Union[IOSample, List[IOSample], Tuple[IOSample]] = None,
        names: List[str] = None,
        data_types: List[tf.DType] = None,
        shapes: List[List[int]] = None,
    ):
        """
        Set the inputs property of this model to be logged along with it. The method 'to_onnx' can use this property as
        well for the conversion process.

        :param from_sample: Read the inputs properties from a given input sample to the model.
        :param names:       List of names for each input layer.
        :param data_types:  List of data types for each input layer.
        :param shapes:      List of tensor shapes for each input layer.
        """
        self._inputs = self._read_ports(
            from_sample=from_sample, names=names, data_types=data_types, shapes=shapes
        )

    def set_outputs(
        self,
        from_sample: Union[IOSample, List[IOSample], Tuple[IOSample]] = None,
        names: List[str] = None,
        data_types: List[tf.DType] = None,
        shapes: List[List[int]] = None,
    ):
        """
        Set the outputs property of this model to be logged along with it. The method 'to_onnx' can use this property as
        well for the conversion process.

        :param from_sample: Read the inputs properties from a given input sample to the model.
        :param names:       List of names for each output layer.
        :param data_types:  List of data types for each output layer.
        :param shapes:      List of tensor shapes for each output layer.
        """
        self._outputs = self._read_ports(
            from_sample=from_sample, names=names, data_types=data_types, shapes=shapes
        )

    # TODO: output_path won't work well with logging artifacts. Need to look into changing the logic of 'log_artifact'.
    def save(
        self, output_path: str = None, *args, **kwargs
    ) -> Union[Dict[str, Artifact], None]:
        """
        Save the handled model at the given output path. If a MLRun context is available, the saved model files will be
        logged and returned as artifacts.

        :param output_path: The full path to the directory to save the handled model at. If not given, the context
                            stored will be used to save the model in the defaulted artifacts location.

        :return The saved model artifacts dictionary if context is available and None otherwise.
        """
        super(TFKerasModelHandler, self).save(output_path=output_path)

        # Setup the returning model artifacts list:
        artifacts = {}  # type: Dict[str, Artifact]
        model_file = None  # type: str
        weights_file = None  # type: str

        # Set the output path:
        if output_path is None:
            output_path = os.path.join(self._context.artifact_path, self._model_name)

        # ModelFormats.H5 - Save as a h5 file:
        if self._model_format == TFKerasModelHandler.ModelFormats.H5:
            model_file = "{}.h5".format(self._model_name)
            self._model.save(model_file)

        # ModelFormats.SAVED_MODEL - Save as a SavedModel directory and zip its file:
        elif self._model_format == TFKerasModelHandler.ModelFormats.SAVED_MODEL:
            # Save it in a SavedModel format directory:
            if self._save_traces is True:
                # Save traces can only be used in versions >= 2.4, so only if its true we use it in the call:
                self._model.save(self._model_name, save_traces=self._save_traces)
            else:
                self._model.save(self._model_name)
            # Zip it:
            model_file = "{}.zip".format(self._model_name)
            shutil.make_archive(
                base_name=self._model_name, format="zip", base_dir=self._model_name
            )

        # ModelFormats.JSON_ARCHITECTURE_H5_WEIGHTS - Save as a json architecture and h5 weights files:
        else:
            # Save the model architecture (json):
            model_architecture = self._model.to_json()
            model_file = "{}.json".format(self._model_name)
            with open(model_file, "w") as json_file:
                json_file.write(model_architecture)
            # Save the model weights (h5):
            weights_file = "{}.h5".format(self._model_name)
            self._model.save_weights(weights_file)

        # Update the paths and log artifacts if context is available:
        self._model_file = model_file
        if self._context is not None:
            artifacts[
                self._get_model_file_artifact_name()
            ] = self._context.log_artifact(
                model_file,
                local_path=model_file,
                artifact_path=output_path,
                db_key=False,
            )
        if weights_file is not None:
            self._weights_file = weights_file
            if self._context is not None:
                artifacts[
                    self._get_weights_file_artifact_name()
                ] = self._context.log_artifact(
                    weights_file,
                    local_path=weights_file,
                    artifact_path=output_path,
                    db_key=False,
                )

        return artifacts if self._context is not None else None

    def load(self, checkpoint: str = None, *args, **kwargs):
        """
        Load the specified model in this handler. If a checkpoint is required to be loaded, it can be given here
        according to the provided model path in the initialization of this handler. Additional parameters for the class
        initializer can be passed via the args list and kwargs dictionary.

        :param checkpoint: The checkpoint label to load the weights from. If the model path is of a store object, the
                           checkpoint will be taken from the logged checkpoints artifacts logged with the model. If the
                           model path is of a local directory, the checkpoint will be searched in it by the provided
                           name to this parameter.
        """
        # TODO: Add support for checkpoint loading after creating MLRun's checkpoint callback.
        if checkpoint is not None:
            raise NotImplementedError(
                "Loading a model using checkpoint is not yet implemented."
            )

        super(TFKerasModelHandler, self).load()

        # ModelFormats.H5 - Load from a .h5 file:
        if self._model_format == TFKerasModelHandler.ModelFormats.H5:
            self._model = keras.models.load_model(
                self._model_file, custom_objects=self._custom_objects
            )

        # ModelFormats.SAVED_MODEL - Load from a SavedModel directory:
        elif self._model_format == TFKerasModelHandler.ModelFormats.SAVED_MODEL:
            self._model = keras.models.load_model(
                self._model_file, custom_objects=self._custom_objects
            )

        # ModelFormats.JSON_ARCHITECTURE_H5_WEIGHTS - Load from a .json architecture file and a .h5 weights file:
        else:
            # Load the model architecture (json):
            with open(self._model_file, "r") as json_file:
                model_architecture = json_file.read()
            self._model = keras.models.model_from_json(
                model_architecture, custom_objects=self._custom_objects
            )
            # Load the model weights (h5):
            self._model.load_weights(self._weights_file)

    def log(
        self,
        labels: Dict[str, Union[str, int, float]] = None,
        parameters: Dict[str, Union[str, int, float]] = None,
        extra_data: Dict[str, Any] = None,
        artifacts: Dict[str, Artifact] = None,
    ):
        """
        Log the model held by this handler into the MLRun context provided.

        :param labels:     Labels to log the model with.
        :param parameters: Parameters to log with the model.
        :param extra_data: Extra data to log with the model.
        :param artifacts:  Artifacts to log the model with. Will be added to the extra data.

        :raise MLRunInvalidArgumentError: In case a context is missing or there is no model in this handler.
        """
        super(TFKerasModelHandler, self).log(
            labels=labels,
            parameters=parameters,
            extra_data=extra_data,
            artifacts=artifacts,
        )

        # Set default values:
        labels = {} if labels is None else labels
        parameters = {} if parameters is None else parameters
        extra_data = {} if extra_data is None else extra_data
        artifacts = {} if artifacts is None else artifacts

        # Save the model:
        model_artifacts = self.save()

        # Log the custom objects:
        custom_objects_artifacts = (
            self._log_custom_objects() if self._custom_objects_map is not None else {}
        )

        # Log the model:
        self._context.log_model(
            self._model_name,
            db_key=self._model_name,
            model_file=self._model_file,
            inputs=self._inputs,
            outputs=self._outputs,
            framework="tf.keras",
            labels={
                "model-format": self._model_format,
                "save-traces": self._save_traces,
                **labels,
            },
            parameters=parameters,
            metrics=self._context.results,
            extra_data={
                **model_artifacts,
                **custom_objects_artifacts,
                **artifacts,
                **extra_data,
            },
        )

    def to_onnx(
        self,
        model_name: str = None,
        input_signature: Union[List[TensorSignature], TensorSignature] = None,
        optimize: bool = True,
        output_path: str = None,
        log: bool = None,
    ):
        """
        Convert the model in this handler to an ONNX model.
        :param model_name:      The name to give to the converted ONNX model. If not given the default name will be the
                                stored model name with the suffix '_onnx'.
        :param input_signature: An numpy.ndarray or tensorflow.TensorSpec that describe the input port (shape and data
                                type). If the model has multiple inputs, a list is expected in the order of the input
                                ports. If not provided, the method will try to extract the input signature of the model.
        :param optimize:        Whether or not to optimize the ONNX model using 'onnxoptimizer' before saving the model.
                                Defaulted to True.
        :param output_path:     In order to save the ONNX model, pass here the output directory. The model file will be
                                named with the model name given. Defaulted to None (not saving).
        :param log:             In order to log the ONNX model, pass True. If None, the model will be logged if this
                                handler has a MLRun context set. Defaulted to None.

        :return: The converted ONNX model (onnx.ModelProto).

        :raise MLRunMissingDependencyError: If the onnx modules are missing in the interpreter.
        :raise MLRunRuntimeError:           If the input signatures couldn't be dicvoered automatically.
        """
        # Import onnx related modules:
        try:
            import tf2onnx

            from mlrun.frameworks.onnx import ONNXModelHandler
        except ModuleNotFoundError:
            raise mlrun.errors.MLRunMissingDependencyError(
                "ONNX conversion requires additional packages to be installed. "
                "Please run 'pip install mlrun[tensorflow]' to install MLRun's Tensorflow package."
            )

        # Set the onnx model name:
        model_name = self._get_default_onnx_model_name(model_name=model_name)

        # Set the input signature:
        if input_signature is None:
            # Try to get the input signature if its not provided:
            try:
                input_signature = [
                    input_layer.type_spec for input_layer in self._model.inputs
                ]
            except Exception as e:
                raise mlrun.errors.MLRunRuntimeError(
                    "Tried to figure out the model's input signature to convert it to ONNX but failed with the "
                    "following message: {}".format(e)
                )
        elif not isinstance(input_signature, list):
            # Wrap it in a list:
            input_signature = [input_signature]

        # Set the output path:
        if output_path is not None:
            output_path = os.path.join(output_path, "{}.onnx".format(model_name))

        # Set the logging flag:
        log = self._context is not None if log is None else log

        # Convert to ONNX:
        model_proto, external_tensor_storage = tf2onnx.convert.from_keras(
            model=self._model, input_signature=input_signature, output_path=output_path
        )

        # Create a handler for the model:
        onnx_handler = ONNXModelHandler(
            model_name=model_name, model=model_proto, context=self._context
        )

        # Optimize the model if needed:
        if optimize:
            onnx_handler.optimize()
            # Save if logging is not required, as logging will save as well:
            if not log and output_path is not None:
                onnx_handler.save(output_path=output_path)

        # Log as a model object if needed:
        if log:
            onnx_handler.log()

    def _collect_files_from_store_object(self):
        """
        If the model path given is of a store object, collect the needed model files into this handler for later loading
        the model.
        """
        # Get the artifact and model file along with its extra data:
        (
            self._model_file,
            self._model_artifact,
            self._extra_data,
        ) = mlrun.artifacts.get_model(self._model_path)

        # Get the model file:  TODO: Once implementing abstract formats, '.pkl' check is only relevant to SavedModel.
        if self._model_file.endswith(".pkl"):
            self._model_file = self._extra_data[
                self._get_model_file_artifact_name()
            ].local()

        # Read the settings:
        self._model_format = self._model_artifact.labels["model-format"]
        self._save_traces = self._model_artifact.labels["save-traces"]

        # Read the IO information:
        self._inputs = self._model_artifact.inputs
        self._outputs = self._model_artifact.outputs

        # Read the custom objects:
        if self._get_custom_objects_map_artifact_name() in self._extra_data:
            self._custom_objects_map = self._extra_data[
                self._get_custom_objects_map_artifact_name()
            ].local()
            self._custom_objects_directory = self._extra_data[
                self._get_custom_objects_directory_artifact_name()
            ].local()
        else:
            self._custom_objects_map = None
            self._custom_objects_directory = None

        # Read additional files according to the model format used:
        # # ModelFormats.SAVED_MODEL - Unzip the SavedModel archive:
        if self._model_format == TFKerasModelHandler.ModelFormats.SAVED_MODEL:
            # Unzip the SavedModel directory:
            with zipfile.ZipFile(self._model_file, "r") as zip_file:
                zip_file.extractall(os.path.dirname(self._model_file))
            # Set the model file to the unzipped directory:
            self._model_file = os.path.join(
                os.path.dirname(self._model_file), self._model_name
            )
        # # ModelFormats.JSON_ARCHITECTURE_H5_WEIGHTS - Get the weights file:
        elif (
            self._model_format
            == TFKerasModelHandler.ModelFormats.JSON_ARCHITECTURE_H5_WEIGHTS
        ):
            # Get the weights file:
            self._weights_file = self._extra_data[
                self._get_weights_file_artifact_name()
            ].local()

    def _collect_files_from_local_path(self):
        """
        If the model path given is of a local path, search for the needed model files and collect them into this handler
        for later loading the model.

        :raise MLRunNotFoundError: If any of the required files are missing.
        """
        # ModelFormats.H5 - Get the h5 model file:
        if self._model_format == TFKerasModelHandler.ModelFormats.H5:
            self._model_file = os.path.join(
                self._model_path, "{}.h5".format(self._model_name)
            )
            if not os.path.exists(self._model_file):
                raise mlrun.errors.MLRunNotFoundError(
                    "The model file '{}.h5' was not found within the given 'model_path': "
                    "'{}'".format(self._model_name, self._model_path)
                )

        # ModelFormats.SAVED_MODEL - Get the zip file and extract it, or simply locate the directory:
        elif self._model_format == TFKerasModelHandler.ModelFormats.SAVED_MODEL:
            self._model_file = os.path.join(
                self._model_path, "{}.zip".format(self._model_name)
            )
            if os.path.exists(self._model_file):
                # Unzip it:
                with zipfile.ZipFile(self._model_file, "r") as zip_file:
                    zip_file.extractall(os.path.dirname(self._model_file))
                # Set the model file to the unzipped directory:
                self._model_file = os.path.join(
                    os.path.dirname(self._model_file), self._model_name
                )
            else:
                # Look for the SavedModel directory:
                self._model_file = os.path.join(self._model_path, self._model_name)
                if not os.path.exists(self._model_file):
                    raise mlrun.errors.MLRunNotFoundError(
                        "There is no SavedModel zip archive '{}' or a SavedModel directory named '{}' the given "
                        "'model_path': '{}'".format(
                            "{}.zip".format(self._model_name),
                            self._model_name,
                            self._model_path,
                        )
                    )

        # ModelFormats.JSON_ARCHITECTURE_H5_WEIGHTS - Save as a json architecture and h5 weights files:
        else:
            # Locate the model architecture json file:
            self._model_file = "{}.json".format(self._model_name)
            if not os.path.exists(os.path.join(self._model_path, self._model_file)):
                raise mlrun.errors.MLRunNotFoundError(
                    "The model architecture file '{}' is missing in the given 'model_path': "
                    "'{}'".format(self._model_file, self._model_path)
                )
            # Locate the model weights h5 file:
            self._weights_file = "{}.h5".format(self._model_name)
            if not os.path.exists(os.path.join(self._model_path, self._weights_file)):
                raise mlrun.errors.MLRunNotFoundError(
                    "The model weights file '{}' is missing in the given 'model_path': "
                    "'{}'".format(self._weights_file, self._model_path)
                )

    @staticmethod
    def _read_ports(
        from_sample: Union[IOSample, List[IOSample], Tuple[IOSample]] = None,
        names: List[str] = None,
        data_types: List[tf.DType] = None,
        shapes: List[List[int]] = None,
    ) -> List[Feature]:
        """
        Generate a list of Features (ports) of a model.

        :param from_sample: Read the ports properties from a given sample to the model.
        :param names:       List of names for each port.
        :param data_types:  List of data types for each port.
        :param shapes:      List of tensor shapes for each port.

        :return: The generated ports list.
        """
        # Initialize the ports list:
        ports = []

        # Check if needed to read from a given sample:
        if from_sample is not None:
            # If there is only one input, wrap in a list:
            if not (isinstance(from_sample, list) or isinstance(from_sample, tuple)):
                from_sample = [from_sample]
            # Go through the inputs and read them:
            for sample in from_sample:
                ports.append(TFKerasModelHandler._read_sample(sample=sample))
        else:
            # Read from given properties:
            for name, data_type, shape in zip(names, data_types, shapes):
                ports.append(Feature(name=name, value_type=data_type.name, dims=shape))

        return ports

    @staticmethod
    def _read_sample(sample: IOSample) -> Feature:
        """
        Read the sample into a MLRun Feature.

        :param sample: The sample to read.

        :return: The created Feature.

        :raise MLRunInvalidArgumentError: In case the given sample type cannot be read.
        """
        if isinstance(sample, np.ndarray):
            # From 'np.ndarray':
            return Feature(value_type=sample.dtype.name, dims=sample.shape)
        elif isinstance(sample, tf.Tensor) or isinstance(sample, tf.TensorSpec):
            return Feature(value_type=sample.dtype.name, dims=list(sample.shape))

        # Unsupported type:
        raise mlrun.errors.MLRunInvalidArgumentError(
            "The sample type given '{}' is not supported.".format(type(sample))
        )
