import os
import sys
import time
import subprocess
import tempfile
import collections
import google.protobuf.text_format
from google.protobuf.descriptor import FieldDescriptor as FD
import torch
import torch.nn as nn
import torch.nn.functional as F
try:
	from urllib2 import urlopen
except:
	from urllib.request import urlopen

caffe_proto = 'https://raw.githubusercontent.com/BVLC/caffe/master/src/caffe/proto/caffe.proto'

TRAIN = 0

TEST = 1

def set_mode_gpu():
	global convert_to_gpu_if_enabled
	convert_to_gpu_if_enabled = lambda obj: obj.cuda()

def set_device(gpu_id):
	torch.cuda.set_device(gpu_id)

class Net(nn.Module):
	def __init__(self, prototxt, *args, **kwargs):
		super(Net, self).__init__()
		# to account for both constructors, see https://github.com/BVLC/caffe/blob/master/python/caffe/test/test_net.py#L145-L147
		caffe_proto_ = kwargs.pop('caffe_proto', None) or caffe_proto
		weights = kwargs.pop('weights', None)
		phase = kwargs.pop('phase', None)
		weights = weights or (args + (None, None))[0]
		phase = phase or (args + (None, None))[1]

		self.net_param = caffe_pb2_singleton(caffe_proto_, codegen_dir).NetParameter()
		google.protobuf.text_format.Parse(open(prototxt).read(), self.net_param)

		def wrap_function(layer_name, forward):
			return type('caffe_' + str(layer_name), (nn.Module, ), dict(forward = lambda self, *inputs: forward(*inputs)))()

		def wrap_caffe_python_layer(layer_name, caffe_python_layer, param_str, caffe_output_variable_names):
			caffe_python_layer.param_str = param_str
			return type('caffe_' + str(layer_name), (nn.Module, ), dict(forward = lambda self, *inputs: CaffePythonLayerFunction(caffe_python_layer, caffe_output_variable_names)(*inputs), __getattr__ = lambda self, name: nn.Module.__getattr__(self, name) if name in dir(self) else getattr(caffe_python_layer, name)))()

		for layer in list(self.net_param.layer) + list(self.net_param.layers):
			layer_type = layer.type if layer.type != 'Python' else layer.python_param.layer
			if isinstance(layer_type, int):
				layer_type = layer.LayerType.Name(layer_type)
			module_constructor = ([v for k, v in modules.items() if k.replace('_', '').upper() in [layer_type.replace('_', '').upper(), layer.name.replace('_', '').upper()]] + [None])[0]
			if module_constructor is not None:
				param = to_dict(([v for f, v in layer.ListFields() if f.name.endswith('_param')] + [None])[0])
				module = module_constructor(param)
				self.add_module(layer.name, module if isinstance(module, nn.Module) else wrap_caffe_python_layer(layer.name, module, param.get('param_str', ''), list(layer.top)) if type(module).__name__.endswith('Layer') else wrap_function(layer.name, module))
				getattr(self, layer.name).caffe_layer_type = layer_type
				getattr(self, layer.name).caffe_input_variable_names = list(layer.bottom)
				getattr(self, layer.name).caffe_output_variable_names = list(layer.top)
				getattr(self, layer.name).caffe_loss_weight = (list(layer.loss_weight) or [1.0 if layer_type.upper().endswith('LOSS') else 0.0]) * len(layer.top)
				getattr(self, layer.name).caffe_optimiziation_params = to_dict(layer.param)
			else:
				print('Skipping layer [{}, {}, {}]: not found in caffemodel2pytorch.modules dict'.format(layer.name, layer_type, layer.type))

		if weights is not None:
			self.copy_from(weights)

		self.blobs = collections.defaultdict(BlobWrapper)
		self.blob_loss_weights = {name : loss_weight for module in self.children() for name, loss_weight in zip(module.caffe_output_variable_names, module.caffe_loss_weight)}

		self.train(phase != TEST)
		convert_to_gpu_if_enabled(self)

	def forward(self, data = None, **variables):
		if data is not None:
			variables['data'] = data
		numpy = not all([isinstance(v, torch.autograd.Variable) for v in variables.values()])
		variables = {k : convert_to_gpu_if_enabled(torch.autograd.Variable(torch.from_numpy(v.copy())) if numpy else v) for k, v in variables.items()}

		for module in [module for module in self.children() if not all([name in variables for name in module.caffe_output_variable_names])]:
			for name in module.caffe_input_variable_names:
				assert name in variables, 'Variable [{}] does not exist. Pass it as a keyword argument or provide a layer which produces it.'.format(name)
			inputs = list(map(variables.get, module.caffe_input_variable_names))
			print(module.caffe_layer_type)
			outputs = module(*inputs)
			if not isinstance(outputs, tuple):
				outputs = (outputs, )
			variables.update(dict(zip(module.caffe_output_variable_names, outputs)))

		self.blobs.update({k : BlobWrapper(data = v, numpy = numpy) for k, v in variables.items()})
		caffe_output_variable_names = set([name for module in self.children() for name in module.caffe_output_variable_names]) - set([name for module in self.children() for name in module.caffe_input_variable_names if name not in module.caffe_output_variable_names])
		return {k : v.detach().cpu().numpy() if numpy else v for k, v in variables.items() if k in caffe_output_variable_names}

	def copy_from(self, weights):
		self.net_param.ParseFromString(open(weights).read())
		for layer in list(self.net_param.layer) + list(self.net_param.layers):
			module = getattr(self, layer.name, None)
			if module is None:
				continue
			parameters = {name : convert_to_gpu_if_enabled(torch.FloatTensor(blob.data)).view(list(blob.shape.dim) if len(blob.shape.dim) > 0 else [blob.num, blob.channels, blob.height, blob.width]) for name, blob in zip(['weight', 'bias'], layer.blobs)}
			if len(parameters) > 0:
				module.set_parameters(**parameters)

	@property
	def layers(self):
		return list(self.children())

class BlobWrapper(object):
	AssignmentAdapter = type('', (object, ), dict(__setitem__ = lambda self, indices, values: setattr(self, 'contents', values)))

	def __init__(self, data = None, numpy = False):
		self.data_ = data if data is not None else BlobWrapper.AssignmentAdapter()
		self.numpy = numpy

	@property
	def data(self):
		if self.numpy and isinstance(self.data_, torch.autograd.Variable):
			self.data_ = self.data_.detach().cpu().numpy()
		return self.data_

	def reshape(self, *args, **kwargs):
		self.shape = args

class Layer(object):
	pass

class CaffePythonLayerFunction(torch.autograd.Function):
	def __init__(self, caffe_python_layer, caffe_output_variable_names):
		self.caffe_python_layer = caffe_python_layer
		self.caffe_output_variable_names = caffe_output_variable_names

	def forward(self, *inputs):
		blobs_in = [BlobWrapper(v.cpu().numpy()) for v in inputs]
		blobs_out = [BlobWrapper() for i in self.caffe_output_variable_names]

		self.caffe_python_layer.setup(blobs_in, blobs_out)
		self.caffe_python_layer.setup = lambda *args: None

		self.caffe_python_layer.forward(blobs_in, blobs_out)
		outputs = tuple(convert_to_gpu_if_enabled(torch.from_numpy(v.data.contents.reshape(*v.shape))) for v in blobs_out)
		return outputs

	def backward(self, grad_output):
		pass
			
class SGDSolver(object):
	def __init__(self, solver_prototxt):
		solver_param = caffe_pb2_singleton(caffe_proto, codegen_dir).SolverParameter()
		google.protobuf.text_format.Parse(open(solver_prototxt).read(), solver_param)
		solver_param = to_dict(solver_param)
		self.net = Net(solver_param.get('train_net') or solver_param.get('net'), phase = TRAIN)
		self.iter = 1
		self.iter_size = solver_param.get('iter_size', 1)
		self.optimizer_params = dict(lr = solver_param.get('base_lr'), momentum = solver_param.get('momentum', 0), weight_decay = solver_param.get('weight_decay', 0))
		self.lr_scheduler_params = dict(policy = solver_param.get('lr_policy'), step_size = solver_param.get('stepsize'), gamma = solver_param.get('gamma'))
		self.optimizer, self.scheduler = None, None

	def init_optimizer_scheduler(self):
		self.optimizer = torch.optim.SGD([dict(params = [param], lr = self.optimizer_params['lr'] * mult.get('lr_mult', 1), weight_decay = self.optimizer_params['weight_decay'] * mult.get('decay_mult', 1), momentum = self.optimizer_params['momentum']) for module in self.net.modules() for param, mult in zip(module.parameters(), module.caffe_optimization_params + [{}, {}])])
		self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size = self.lr_scheduler_params['step_size'], gamma = self.lr_scheduler_params['gamma']) if solver_param.get('policy') == 'step' else type('', (object, ), dict(step = lambda self: None))()

	def step(self, iterations = 1, **inputs):
		loss_total = 0.0
		for i in range(iterations):
			if self.optimizer is not None:
				self.optimizer.zero_grad()

			for j in range(self.iter_size):
				outputs = self.net(**inputs)
				loss = sum([self.net.blob_loss_weights[k] * v.sum() for k, v in outputs.items()])
				loss_total += float(loss)
				if self.optimizer is None:
					self.init_optimizer_scheduler()
					self.optimizer.zero_grad()
				loss.backward()

			self.optimizer.step()
			self.scheduler.step()
			self.iter += 1
		return loss_total

modules = dict(
	Convolution = lambda param: Convolution(param),
	InnerProduct = lambda param: InnerProduct(param),
	Pooling = lambda param: [nn.MaxPool2d, nn.AvgPool2d][param['pool']](kernel_size = first_or(param, 'kernel_size', 1), stride = first_or(param, 'stride', 1), padding = first_or(param, 'pad', 0)),
	Softmax = lambda param: nn.Softmax(dim = param['axis'] if param else -1),
	ReLU = lambda ignored: nn.ReLU(),
	Dropout = lambda param: nn.Dropout(param['dropout_ratio']),
	Eltwise = lambda param: [torch.mul, torch.add, torch.max][param['operation']]
)

class Convolution(nn.Conv2d):
	def __init__(self, param):
		super(Convolution, self).__init__(1, param['num_output'], kernel_size = first_or(param, 'kernel_size', 1), stride = first_or(param, 'stride', 1), padding = first_or(param, 'pad', 0), dilation = first_or(param, 'dilation', 1))
		self.weight, self.bias = None, None
		self.weight_init, self.bias_init = param.get('weight_filler', {}), param.get('bias_filler', {})

	def forward(self, x):
		if self.weight is None and self.bias is None:
			super(Convolution, self).__init__(x.size(1), self.out_channels, kernel_size = self.kernel_size, stride = self.stride, padding = self.padding, dilation = self.dilation)
			convert_to_gpu_if_enabled(self)
			init_weight_bias(self)
		return super(Convolution, self).forward(x)

	def set_parameters(self, weight = None, bias = None):
		if weight is not None:
			self.weight = nn.Parameter(weight)
			self.in_channels = self.weight.size(1)

		if bias is not None:
			self.bias = nn.Parameter(bias.view(-1))

class InnerProduct(nn.Linear):
	def __init__(self, param):
		super(InnerProduct, self).__init__(1, param['num_output'])
		self.weight, self.bias = None, None
		self.weight_init, self.bias_init = param.get('weight_filler', {}), param.get('bias_filler', {})

	def forward(self, x):
		if self.weight is None and self.bias is None:
			super(InnerProduct, self).__init__(x.size(1), self.out_features)
			convert_to_gpu_if_enabled(self)
			init_weight_bias(self)
		return super(InnerProduct, self).forward(x if x.size(-1) == self.in_features else x.view(len(x), -1))

	def set_parameters(self, weight = None, bias = None):
		if weight is not None:
			self.weight = nn.Parameter(weight.view(weight.size(-2), weight.size(-1)))
			self.in_features = self.weight.size(1)

		if bias is not None:
			self.bias = nn.Parameter(bias.view(-1))

def init_weight_bias(self):
	for name in ['weight', 'bias']:
		tensor, init = getattr(self, name), getattr(self, name + '_init')
		if init.get('type') == 'gaussian':
			nn.init.normal(tensor, std = init['std'])
		elif init.get('type') == 'constant':
			nn.init.constant(tensor, val = init['value'])

codegen_dir = tempfile.mkdtemp()

def caffe_pb2_singleton(caffe_proto, codegen_dir):
	if codegen_dir != sys.path[0]:
		local_caffe_proto = os.path.join(codegen_dir, os.path.basename(caffe_proto))
		with open(local_caffe_proto, 'w') as f:
			f.write((urlopen if 'http' in caffe_proto else open)(caffe_proto).read())
		subprocess.check_call(['protoc', '--proto_path', os.path.dirname(local_caffe_proto), '--python_out', codegen_dir, local_caffe_proto])
		sys.path.insert(0, codegen_dir)
		import caffe_pb2
	return sys.modules['caffe_pb2']

def convert_to_gpu_if_enabled(obj):
	return obj

def first_or(param, key, default):
	return param[key] if isinstance(param.get(key), int) else (param.get(key, []) + [default])[0]
		
def to_dict(obj):
	return list(map(to_dict, obj)) if isinstance(obj, collections.Iterable) else {} if obj is None else {f.name : converter(v) if f.label != FD.LABEL_REPEATED else list(map(converter, v)) for f, v in obj.ListFields() for converter in [{FD.TYPE_DOUBLE: float, FD.TYPE_SFIXED32: float, FD.TYPE_SFIXED64: float, FD.TYPE_SINT32: int, FD.TYPE_SINT64: long, FD.TYPE_FLOAT: float, FD.TYPE_ENUM: int, FD.TYPE_UINT32: int, FD.TYPE_INT64: long, FD.TYPE_UINT64: long, FD.TYPE_INT32: int, FD.TYPE_FIXED64: float, FD.TYPE_FIXED32: float, FD.TYPE_BOOL: bool, FD.TYPE_STRING: unicode, FD.TYPE_BYTES: lambda x: x.encode('string_escape'), FD.TYPE_MESSAGE: to_dict}[f.type]]}