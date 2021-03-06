from contextlib import contextmanager
import sys

from _pydev_imps._pydev_saved_modules import threading
from _pydevd_bundle.pydevd_constants import get_frame, dict_items, RETURN_VALUES_DICT, \
    dict_iter_items
import traceback
from _pydevd_bundle.pydevd_xml import get_variable_details, get_type
from _pydev_bundle.pydev_override import overrides
from _pydevd_bundle.pydevd_resolver import sorted_attributes_key
from _pydevd_bundle.pydevd_safe_repr import SafeRepr


class _AbstractVariable(object):

    # Default attributes in class, set in instance.

    name = None
    value = None
    evaluate_name = None

    def get_name(self):
        return self.name

    def get_value(self):
        return self.value

    def get_variable_reference(self):
        return id(self.value)

    def get_var_data(self, fmt=None):
        '''
        :param dict fmt:
            Format expected by the DAP (keys: 'hex': bool, 'rawString': bool)
        '''
        safe_repr = SafeRepr()
        if fmt is not None:
            safe_repr.convert_to_hex = fmt.get('hex', False)
            safe_repr.raw_value = fmt.get('rawString', False)

        type_name, _type_qualifier, _is_exception_on_eval, resolver, value = get_variable_details(
            self.value, to_string=safe_repr)

        is_raw_string = type_name in ('str', 'unicode', 'bytes', 'bytearray')

        attributes = []

        if is_raw_string:
            attributes.append('rawString')

        name = self.name

        if self._is_return_value:
            attributes.append('readOnly')
            name = '(return) %s' % (name,)

        var_data = {
            'name': name,
            'value': value,
            'type': type_name,
        }

        if self.evaluate_name is not None:
            var_data['evaluateName'] = self.evaluate_name

        if resolver is not None:  # I.e.: it's a container
            var_data['variablesReference'] = self.get_variable_reference()

        if len(attributes) > 0:
            var_data['presentationHint'] = {'attributes': attributes}

        return var_data

    def get_children_variables(self, fmt=None):
        raise NotImplementedError()


class _ObjectVariable(_AbstractVariable):

    def __init__(self, name, value, register_variable, is_return_value=False, evaluate_name=None):
        _AbstractVariable.__init__(self)
        self.name = name
        self.value = value
        self._register_variable = register_variable
        self._register_variable(self)
        self._is_return_value = is_return_value
        self.evaluate_name = evaluate_name

    @overrides(_AbstractVariable.get_children_variables)
    def get_children_variables(self, fmt=None):
        _type, _type_name, resolver = get_type(self.value)

        children_variables = []
        if resolver is not None:  # i.e.: it's a container.
            if hasattr(resolver, 'get_contents_debug_adapter_protocol'):
                # The get_contents_debug_adapter_protocol needs to return sorted.
                lst = resolver.get_contents_debug_adapter_protocol(self.value, fmt=fmt)
            else:
                # If there's no special implementation, the default is sorting the keys.
                dct = resolver.get_dictionary(self.value)
                lst = dict_items(dct)
                lst.sort(key=lambda tup: sorted_attributes_key(tup[0]))
                # No evaluate name in this case.
                lst = [(key, value, None) for (key, value) in lst]

            parent_evaluate_name = self.evaluate_name
            if parent_evaluate_name:
                for key, val, evaluate_name in lst:
                    if evaluate_name is not None:
                        if callable(evaluate_name):
                            evaluate_name = evaluate_name(parent_evaluate_name)
                        else:
                            evaluate_name = parent_evaluate_name + evaluate_name
                    variable = _ObjectVariable(
                        key, val, self._register_variable, evaluate_name=evaluate_name)
                    children_variables.append(variable)
            else:
                for key, val, evaluate_name in lst:
                    # No evaluate name
                    variable = _ObjectVariable(key, val, self._register_variable)
                    children_variables.append(variable)

        return children_variables


def sorted_variables_key(obj):
    return sorted_attributes_key(obj.name)


class _FrameVariable(_AbstractVariable):

    def __init__(self, frame, register_variable):
        _AbstractVariable.__init__(self)
        self.frame = frame

        self.name = self.frame.f_code.co_name
        self.value = frame

        self._register_variable = register_variable
        self._register_variable(self)

    @overrides(_AbstractVariable.get_children_variables)
    def get_children_variables(self, fmt=None):
        children_variables = []
        for key, val in dict_items(self.frame.f_locals):
            is_return_value = key == RETURN_VALUES_DICT
            if is_return_value:
                for return_key, return_value in dict_iter_items(val):
                    variable = _ObjectVariable(
                        return_key, return_value, self._register_variable, is_return_value, '%s[%r]' % (key, return_key))
                    children_variables.append(variable)
            else:
                variable = _ObjectVariable(key, val, self._register_variable, is_return_value, key)
                children_variables.append(variable)

        # Frame variables always sorted.
        children_variables.sort(key=sorted_variables_key)

        return children_variables


class _FramesTracker(object):
    '''
    This is a helper class to be used to track frames when a thread becomes suspended.
    '''

    def __init__(self, suspended_frames_manager, py_db):
        self._suspended_frames_manager = suspended_frames_manager
        self.py_db = py_db
        self._frame_id_to_frame = {}

        # Note that a given frame may appear in multiple threads when we have custom
        # frames added, but as those are coroutines, this map will point to the actual
        # main thread (which is the one that needs to be suspended for us to get the
        # variables).
        self._frame_id_to_main_thread_id = {}

        # A map of the suspended thread id -> list(frames ids) -- note that
        # frame ids are kept in order (the first one is the suspended frame).
        self._thread_id_to_frame_ids = {}

        # A map of the lines where it's suspended (needed for exceptions where the frame
        # lineno is not correct).
        self._frame_id_to_lineno = {}

        # The main suspended thread (if this is a coroutine this isn't the id of the
        # coroutine thread, it's the id of the actual suspended thread).
        self._main_thread_id = None

        # Helper to know if it was already untracked.
        self._untracked = False

        # We need to be thread-safe!
        self._lock = threading.Lock()

        self._variable_reference_to_variable = {}

    def _register_variable(self, variable):
        variable_reference = variable.get_variable_reference()
        self._variable_reference_to_variable[variable_reference] = variable

    def obtain_as_variable(self, name, value, evaluate_name=None):
        if evaluate_name is None:
            evaluate_name = name

        variable_reference = id(value)
        variable = self._variable_reference_to_variable.get(variable_reference)
        if variable is not None:
            return variable

        # Still not created, let's do it now.
        return _ObjectVariable(
            name, value, self._register_variable, is_return_value=False, evaluate_name=evaluate_name)

    def get_main_thread_id(self):
        return self._main_thread_id

    def get_variable(self, variable_reference):
        return self._variable_reference_to_variable[variable_reference]

    def track(self, thread_id, frame, frame_id_to_lineno, frame_custom_thread_id=None):
        '''
        :param thread_id:
            The thread id to be used for this frame.

        :param frame:
            The topmost frame which is suspended at the given thread.

        :param frame_id_to_lineno:
            If available, the line number for the frame will be gotten from this dict,
            otherwise frame.f_lineno will be used (needed for unhandled exceptions as
            the place where we report may be different from the place where it's raised).

        :param frame_custom_thread_id:
            If None this this is the id of the thread id for the custom frame (i.e.: coroutine).
        '''
        with self._lock:
            coroutine_or_main_thread_id = frame_custom_thread_id or thread_id

            if coroutine_or_main_thread_id in self._suspended_frames_manager._thread_id_to_tracker:
                sys.stderr.write('pydevd: Something is wrong. Tracker being added twice to the same thread id.\n')

            self._suspended_frames_manager._thread_id_to_tracker[coroutine_or_main_thread_id] = self
            self._main_thread_id = thread_id
            self._frame_id_to_lineno = frame_id_to_lineno

            frame_ids_from_thread = self._thread_id_to_frame_ids.setdefault(
                coroutine_or_main_thread_id, [])

            while frame is not None:
                frame_id = id(frame)
                self._frame_id_to_frame[frame_id] = frame
                _FrameVariable(frame, self._register_variable)  # Instancing is enough to register.
                self._suspended_frames_manager._variable_reference_to_frames_tracker[frame_id] = self
                frame_ids_from_thread.append(frame_id)

                self._frame_id_to_main_thread_id[frame_id] = thread_id

                frame = frame.f_back

    def untrack_all(self):
        with self._lock:
            if self._untracked:
                # Calling multiple times is expected for the set next statement.
                return
            self._untracked = True
            for thread_id in self._thread_id_to_frame_ids:
                self._suspended_frames_manager._thread_id_to_tracker.pop(thread_id, None)

            for frame_id in self._frame_id_to_frame:
                del self._suspended_frames_manager._variable_reference_to_frames_tracker[frame_id]

            self._frame_id_to_frame.clear()
            self._frame_id_to_main_thread_id.clear()
            self._thread_id_to_frame_ids.clear()
            self._frame_id_to_lineno.clear()
            self._main_thread_id = None
            self._suspended_frames_manager = None
            self._variable_reference_to_variable.clear()

    def get_topmost_frame_and_frame_id_to_line(self, thread_id):
        with self._lock:
            frame_ids = self._thread_id_to_frame_ids.get(thread_id)
            if frame_ids is not None:
                frame_id = frame_ids[0]
                return self._frame_id_to_frame[frame_id], self._frame_id_to_lineno

    def find_frame(self, thread_id, frame_id):
        with self._lock:
            return self._frame_id_to_frame.get(frame_id)

    def create_thread_suspend_command(self, thread_id, stop_reason, message, suspend_type):
        with self._lock:
            frame_ids = self._thread_id_to_frame_ids[thread_id]

            # First one is topmost frame suspended.
            frame = self._frame_id_to_frame[frame_ids[0]]

            cmd = self.py_db.cmd_factory.make_thread_suspend_message(
                thread_id, frame, stop_reason, message, suspend_type, frame_id_to_lineno=self._frame_id_to_lineno)

            frame = None
            return cmd


class SuspendedFramesManager(object):

    def __init__(self):
        self._thread_id_to_fake_frames = {}
        self._thread_id_to_tracker = {}

        # Mappings
        self._variable_reference_to_frames_tracker = {}

    def _get_tracker_for_variable_reference(self, variable_reference):
        tracker = self._variable_reference_to_frames_tracker.get(variable_reference)
        if tracker is not None:
            return tracker

        for _thread_id, tracker in dict_iter_items(self._thread_id_to_tracker):
            try:
                tracker.get_variable(variable_reference)
            except KeyError:
                pass
            else:
                return tracker

        return None

    def get_thread_id_for_variable_reference(self, variable_reference):
        '''
        We can't evaluate variable references values on any thread, only in the suspended
        thread (the main reason for this is that in UI frameworks inspecting a UI object
        from a different thread can potentially crash the application).

        :param int variable_reference:
            The variable reference (can be either a frame id or a reference to a previously
            gotten variable).

        :return str:
            The thread id for the thread to be used to inspect the given variable reference or
            None if the thread was already resumed.
        '''
        frames_tracker = self._get_tracker_for_variable_reference(variable_reference)
        if frames_tracker is not None:
            return frames_tracker.get_main_thread_id()
        return None

    def get_frame_tracker(self, thread_id):
        return self._thread_id_to_tracker.get(thread_id)

    def get_variable(self, variable_reference):
        '''
        :raises KeyError
        '''
        frames_tracker = self._get_tracker_for_variable_reference(variable_reference)
        if frames_tracker is None:
            raise KeyError()
        return frames_tracker.get_variable(variable_reference)

    def get_topmost_frame_and_frame_id_to_line(self, thread_id):
        tracker = self._thread_id_to_tracker.get(thread_id)
        if tracker is None:
            return None
        return tracker.get_topmost_frame_and_frame_id_to_line(thread_id)

    @contextmanager
    def track_frames(self, py_db):
        tracker = _FramesTracker(self, py_db)
        try:
            yield tracker
        finally:
            tracker.untrack_all()

    def add_fake_frame(self, thread_id, frame_id, frame):
        self._thread_id_to_fake_frames.setdefault(thread_id, {})[int(frame_id)] = frame

    def find_frame(self, thread_id, frame_id):
        try:
            if frame_id == "*":
                return get_frame()  # any frame is specified with "*"
            frame_id = int(frame_id)

            fake_frames = self._thread_id_to_fake_frames.get(thread_id)
            if fake_frames is not None:
                frame = fake_frames.get(frame_id)
                if frame is not None:
                    return frame

            frames_tracker = self._thread_id_to_tracker.get(thread_id)
            if frames_tracker is not None:
                frame = frames_tracker.find_frame(thread_id, frame_id)
                if frame is not None:
                    return frame

            return None
        except:
            traceback.print_exc()
            return None
