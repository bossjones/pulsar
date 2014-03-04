import sys
import unittest
import logging
from unittest.case import _ExpectedFailure as ExpectedFailure

from pulsar import multi_async, coroutine_return
from pulsar.utils.pep import ispy3k
from pulsar.apps import tasks

from .utils import TestFunction, TestFailure

if ispy3k:
    from unittest import mock
else:  # pragma nocover
    try:
        import mock
    except ImportError:
        mock = None


LOGGER = logging.getLogger('pulsar.test')


class Test(tasks.Job):
    '''A :class:`.Job` for running tests on a task queue.
    '''
    def __call__(self, consumer, testcls=None, tag=None):
        runner = consumer.worker.app.new_runner()
        if not isinstance(testcls, type):
            testcls = testcls()
        testcls.tag = tag
        testcls.cfg = consumer.worker.cfg
        all_tests = runner.loadTestsFromTestCase(testcls)
        num = all_tests.countTestCases()
        if num:
            return self.run(consumer, runner, testcls, all_tests)
        else:
            return runner.result

    def create_id(self, kwargs):
        tid = super(Test, self).create_id(kwargs)
        testcls = kwargs.get('testcls')
        return '%s_%s' % (testcls.__name__, tid) if testcls else tid

    def run(self, consumer, runner, testcls, all_tests):
        '''Run all test functions from the :attr:`testcls`.

        It uses the following algorithm:

        * Run the class method ``setUpClass`` of :attr:`testcls` if defined,
          unless the test class should be skipped
        * Call :meth:`run_test` for each test functions in :attr:`testcls`
        * Run the class method ``tearDownClass`` of :attr:`testcls` if defined,
          unless the test class should be skipped.
        '''
        cfg = testcls.cfg
        loop = consumer._loop
        runner.startTestClass(testcls)
        error = None
        sequential = getattr(testcls, '_sequential_execution', cfg.sequential)
        skip_tests = getattr(testcls, '__unittest_skip__', False)
        if not skip_tests:
            error = yield self._run(runner, testcls, 'setUpClass',
                                    add_err=False)
        # run the tests
        if not error:
            if sequential:
                # Loop over all test cases in class
                for test in all_tests:
                    yield self.run_test(test, runner)
            else:
                all = (self.run_test(test, runner) for test in all_tests)
                yield multi_async(all, loop=loop)
        else:
            for test in all_tests:
                runner.startTest(test)
                self.add_failure(test, runner, error)
                runner.stopTest(test)
        if not skip_tests:
            yield self._run(runner, testcls, 'tearDownClass', add_err=False)
        runner.stopTestClass(testcls)
        coroutine_return(runner.result)

    def run_test(self, test, runner):
        '''Run a ``test`` function using the following algorithm

        * Run :meth:`_pre_setup` method if available in :attr:`testcls`.
        * Run :meth:`setUp` method in :attr:`testcls`.
        * Run the test function.
        * Run :meth:`tearDown` method in :attr:`testcls`.
        * Run :meth:`_post_teardown` method if available in :attr:`testcls`.
        '''
        err = None
        try:
            runner.startTest(test)
            testMethod = getattr(test, test._testMethodName)
            if (getattr(test.__class__, '__unittest_skip__', False) or
                    getattr(testMethod, '__unittest_skip__', False)):
                reason = (getattr(test.__class__,
                                  '__unittest_skip_why__', '') or
                          getattr(testMethod,
                                  '__unittest_skip_why__', ''))
                runner.addSkip(test, reason)
                err = True
            else:
                err = yield self._run(runner, test, '_pre_setup')
                if not err:
                    err = yield self._run(runner, test, 'setUp')
                    if not err:
                        err = yield self._run(runner, test,
                                              test._testMethodName)
                    err = yield self._run(runner, test, 'tearDown', err)
                err = yield self._run(runner, test, '_post_teardown', err)
                runner.stopTest(test)
        except Exception as error:
            self.add_failure(test, runner, error, err)
        else:
            if not err:
                runner.addSuccess(test)

    def _run(self, runner, test, method, previous=None, add_err=True):
        __skip_traceback__ = True
        method = getattr(test, method, None)
        if method:
            # Check if a testfunction object is already available
            # Check the run_on_arbiter decorator for information
            tfunc = getattr(method, 'testfunction', None)
            if tfunc is None:
                tfunc = TestFunction(method.__name__)
            try:
                exc = yield tfunc(test, test.cfg.test_timeout)
            except Exception as e:
                exc = e
            if exc:
                add_err = False if previous else add_err
                previous = self.add_failure(test, runner, exc, add_err)
        coroutine_return(previous)

    def add_failure(self, test, runner, failure, add_err=True):
        '''Add ``error`` to the list of errors.

        :param test: the test function object where the error occurs
        :param runner: the test runner
        :param error: the python exception for the error
        :param add_err: if ``True`` the error is added to the list of errors
        :return: a tuple containing the ``error`` and the ``exc_info``
        '''
        if add_err:
            if not isinstance(failure, TestFailure):
                failure = TestFailure(failure)
            if isinstance(failure.exc, test.failureException):
                runner.addFailure(test, failure)
            elif isinstance(failure.exc, ExpectedFailure):
                runner.addExpectedFailure(test, failure)
            else:
                runner.addError(test, failure)
        else:
            LOGGER.exception('exception')
        return failure
