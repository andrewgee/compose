from __future__ import absolute_import
from __future__ import unicode_literals

import logging
import operator
import sys
from threading import Thread

from docker.errors import APIError
from six.moves import _thread as thread
from six.moves.queue import Empty
from six.moves.queue import Queue

from compose.cli.signals import ShutdownException
from compose.errors import OperationFailedError
from compose.utils import get_output_stream


log = logging.getLogger(__name__)

STOP = object()


def parallel_execute(objects, func, get_name, msg, get_deps=None):
    """Runs func on objects in parallel while ensuring that func is
    ran on object only after it is ran on all its dependencies.

    get_deps called on object must return a collection with its dependencies.
    get_name called on object must return its name.
    """
    objects = list(objects)
    stream = get_output_stream(sys.stderr)

    writer = ParallelStreamWriter(stream, msg)
    for obj in objects:
        writer.initialize(get_name(obj))

    events = parallel_execute_iter(objects, func, get_deps)

    errors = {}
    results = []
    error_to_reraise = None

    for obj, result, exception in events:
        if exception is None:
            writer.write(get_name(obj), 'done')
            results.append(result)
        elif isinstance(exception, APIError):
            errors[get_name(obj)] = exception.explanation
            writer.write(get_name(obj), 'error')
        elif isinstance(exception, OperationFailedError):
            errors[get_name(obj)] = exception.msg
            writer.write(get_name(obj), 'error')
        elif isinstance(exception, UpstreamError):
            writer.write(get_name(obj), 'error')
        else:
            errors[get_name(obj)] = exception
            error_to_reraise = exception

    for obj_name, error in errors.items():
        stream.write("\nERROR: for {}  {}\n".format(obj_name, error))

    if error_to_reraise:
        raise error_to_reraise

    return results, errors


def _no_deps(x):
    return []


class State(object):
    """
    Holds the state of a partially-complete parallel operation.

    state.started:   objects being processed
    state.finished:  objects which have been processed
    state.failed:    objects which either failed or whose dependencies failed
    """
    def __init__(self, objects):
        self.objects = objects

        self.started = set()
        self.finished = set()
        self.failed = set()

    def is_done(self):
        return len(self.finished) + len(self.failed) >= len(self.objects)

    def pending(self):
        return set(self.objects) - self.started - self.finished - self.failed


def parallel_execute_iter(objects, func, get_deps):
    """
    Runs func on objects in parallel while ensuring that func is
    ran on object only after it is ran on all its dependencies.

    Returns an iterator of tuples which look like:

    # if func returned normally when run on object
    (object, result, None)

    # if func raised an exception when run on object
    (object, None, exception)

    # if func raised an exception when run on one of object's dependencies
    (object, None, UpstreamError())
    """
    if get_deps is None:
        get_deps = _no_deps

    results = Queue()
    state = State(objects)

    while True:
        feed_queue(objects, func, get_deps, results, state)

        try:
            event = results.get(timeout=0.1)
        except Empty:
            continue
        # See https://github.com/docker/compose/issues/189
        except thread.error:
            raise ShutdownException()

        if event is STOP:
            break

        obj, _, exception = event
        if exception is None:
            log.debug('Finished processing: {}'.format(obj))
            state.finished.add(obj)
        else:
            log.debug('Failed: {}'.format(obj))
            state.failed.add(obj)

        yield event


def producer(obj, func, results):
    """
    The entry point for a producer thread which runs func on a single object.
    Places a tuple on the results queue once func has either returned or raised.
    """
    try:
        result = func(obj)
        results.put((obj, result, None))
    except Exception as e:
        results.put((obj, None, e))


def feed_queue(objects, func, get_deps, results, state):
    """
    Starts producer threads for any objects which are ready to be processed
    (i.e. they have no dependencies which haven't been successfully processed).

    Shortcuts any objects whose dependencies have failed and places an
    (object, None, UpstreamError()) tuple on the results queue.
    """
    pending = state.pending()
    log.debug('Pending: {}'.format(pending))

    for obj in pending:
        deps = get_deps(obj)

        if any(dep in state.failed for dep in deps):
            log.debug('{} has upstream errors - not processing'.format(obj))
            results.put((obj, None, UpstreamError()))
            state.failed.add(obj)
        elif all(
            dep not in objects or dep in state.finished
            for dep in deps
        ):
            log.debug('Starting producer thread for {}'.format(obj))
            t = Thread(target=producer, args=(obj, func, results))
            t.daemon = True
            t.start()
            state.started.add(obj)

    if state.is_done():
        results.put(STOP)


class UpstreamError(Exception):
    pass


class ParallelStreamWriter(object):
    """Write out messages for operations happening in parallel.

    Each operation has it's own line, and ANSI code characters are used
    to jump to the correct line, and write over the line.
    """

    def __init__(self, stream, msg):
        self.stream = stream
        self.msg = msg
        self.lines = []

    def initialize(self, obj_index):
        if self.msg is None:
            return
        self.lines.append(obj_index)
        self.stream.write("{} {} ... \r\n".format(self.msg, obj_index))
        self.stream.flush()

    def write(self, obj_index, status):
        if self.msg is None:
            return
        position = self.lines.index(obj_index)
        diff = len(self.lines) - position
        # move up
        self.stream.write("%c[%dA" % (27, diff))
        # erase
        self.stream.write("%c[2K\r" % 27)
        self.stream.write("{} {} ... {}\r".format(self.msg, obj_index, status))
        # move back down
        self.stream.write("%c[%dB" % (27, diff))
        self.stream.flush()


def parallel_operation(containers, operation, options, message):
    results, errors = parallel_execute(
        containers,
        operator.methodcaller(operation, **options),
        operator.attrgetter('name'),
        message)
    return results, errors


def parallel_remove(containers, options):
    stopped_containers = [c for c in containers if not c.is_running]
    results, errors = parallel_operation(stopped_containers, 'remove', options, 'Removing')
    return results, errors


def parallel_start(containers, options):
    results, errors = parallel_operation(containers, 'start', options, 'Starting')
    return results, errors


def parallel_pause(containers, options):
    results, errors = parallel_operation(containers, 'pause', options, 'Pausing')
    return results, errors


def parallel_unpause(containers, options):
    results, errors = parallel_operation(containers, 'unpause', options, 'Unpausing')
    return results, errors


def parallel_kill(containers, options):
    results, errors = parallel_operation(containers, 'kill', options, 'Killing')
    return results, errors


def parallel_restart(containers, options):
    results, errors = parallel_operation(containers, 'restart', options, 'Restarting')
    return results, errors
