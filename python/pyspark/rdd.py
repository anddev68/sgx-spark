import atexit
from base64 import standard_b64encode as b64enc
import copy
from collections import defaultdict
from itertools import chain, ifilter, imap, product
import operator
import os
import shlex
from subprocess import Popen, PIPE
from tempfile import NamedTemporaryFile
from threading import Thread

from pyspark import cloudpickle
from pyspark.serializers import batched, Batch, dump_pickle, load_pickle, \
    read_from_pickle_file
from pyspark.join import python_join, python_left_outer_join, \
    python_right_outer_join, python_cogroup

from py4j.java_collections import ListConverter, MapConverter


__all__ = ["RDD"]


class RDD(object):
    """
    A Resilient Distributed Dataset (RDD), the basic abstraction in Spark.
    Represents an immutable, partitioned collection of elements that can be
    operated on in parallel.
    """

    def __init__(self, jrdd, ctx):
        self._jrdd = jrdd
        self.is_cached = False
        self.ctx = ctx

    @property
    def context(self):
        """
        The L{SparkContext} that this RDD was created on.
        """
        return self.ctx

    def cache(self):
        """
        Persist this RDD with the default storage level (C{MEMORY_ONLY}).
        """
        self.is_cached = True
        self._jrdd.cache()
        return self

    # TODO persist(self, storageLevel)

    def map(self, f, preservesPartitioning=False):
        """
        Return a new RDD containing the distinct elements in this RDD.
        """
        def func(iterator): return imap(f, iterator)
        return PipelinedRDD(self, func, preservesPartitioning)

    def flatMap(self, f, preservesPartitioning=False):
        """
        Return a new RDD by first applying a function to all elements of this
        RDD, and then flattening the results.

        >>> rdd = sc.parallelize([2, 3, 4])
        >>> sorted(rdd.flatMap(lambda x: range(1, x)).collect())
        [1, 1, 1, 2, 2, 3]
        >>> sorted(rdd.flatMap(lambda x: [(x, x), (x, x)]).collect())
        [(2, 2), (2, 2), (3, 3), (3, 3), (4, 4), (4, 4)]
        """
        def func(iterator): return chain.from_iterable(imap(f, iterator))
        return self.mapPartitions(func, preservesPartitioning)

    def mapPartitions(self, f, preservesPartitioning=False):
        """
        Return a new RDD by applying a function to each partition of this RDD.

        >>> rdd = sc.parallelize([1, 2, 3, 4], 2)
        >>> def f(iterator): yield sum(iterator)
        >>> rdd.mapPartitions(f).collect()
        [3, 7]
        """
        return PipelinedRDD(self, f, preservesPartitioning)

    # TODO: mapPartitionsWithSplit

    def filter(self, f):
        """
        Return a new RDD containing only the elements that satisfy a predicate.

        >>> rdd = sc.parallelize([1, 2, 3, 4, 5])
        >>> rdd.filter(lambda x: x % 2 == 0).collect()
        [2, 4]
        """
        def func(iterator): return ifilter(f, iterator)
        return self.mapPartitions(func)

    def distinct(self):
        """
        Return a new RDD containing the distinct elements in this RDD.

        >>> sorted(sc.parallelize([1, 1, 2, 3]).distinct().collect())
        [1, 2, 3]
        """
        return self.map(lambda x: (x, "")) \
                   .reduceByKey(lambda x, _: x) \
                   .map(lambda (x, _): x)

    # TODO: sampling needs to be re-implemented due to Batch
    #def sample(self, withReplacement, fraction, seed):
    #    jrdd = self._jrdd.sample(withReplacement, fraction, seed)
    #    return RDD(jrdd, self.ctx)

    #def takeSample(self, withReplacement, num, seed):
    #    vals = self._jrdd.takeSample(withReplacement, num, seed)
    #    return [load_pickle(bytes(x)) for x in vals]

    def union(self, other):
        """
        Return the union of this RDD and another one.

        >>> rdd = sc.parallelize([1, 1, 2, 3])
        >>> rdd.union(rdd).collect()
        [1, 1, 2, 3, 1, 1, 2, 3]
        """
        return RDD(self._jrdd.union(other._jrdd), self.ctx)

    def __add__(self, other):
        """
        Return the union of this RDD and another one.

        >>> rdd = sc.parallelize([1, 1, 2, 3])
        >>> (rdd + rdd).collect()
        [1, 1, 2, 3, 1, 1, 2, 3]
        """
        if not isinstance(other, RDD):
            raise TypeError
        return self.union(other)

    # TODO: sort

    def glom(self):
        """
        Return an RDD created by coalescing all elements within each partition
        into a list.

        >>> rdd = sc.parallelize([1, 2, 3, 4], 2)
        >>> sorted(rdd.glom().collect())
        [[1, 2], [3, 4]]
        """
        def func(iterator): yield list(iterator)
        return self.mapPartitions(func)

    def cartesian(self, other):
        """
        Return the Cartesian product of this RDD and another one, that is, the
        RDD of all pairs of elements C{(a, b)} where C{a} is in C{self} and
        C{b} is in C{other}.

        >>> rdd = sc.parallelize([1, 2])
        >>> sorted(rdd.cartesian(rdd).collect())
        [(1, 1), (1, 2), (2, 1), (2, 2)]
        """
        # Due to batching, we can't use the Java cartesian method.
        java_cartesian = RDD(self._jrdd.cartesian(other._jrdd), self.ctx)
        def unpack_batches(pair):
            (x, y) = pair
            if type(x) == Batch or type(y) == Batch:
                xs = x.items if type(x) == Batch else [x]
                ys = y.items if type(y) == Batch else [y]
                for pair in product(xs, ys):
                    yield pair
            else:
                yield pair
        return java_cartesian.flatMap(unpack_batches)

    def groupBy(self, f, numSplits=None):
        """
        Return an RDD of grouped items.

        >>> rdd = sc.parallelize([1, 1, 2, 3, 5, 8])
        >>> result = rdd.groupBy(lambda x: x % 2).collect()
        >>> sorted([(x, sorted(y)) for (x, y) in result])
        [(0, [2, 8]), (1, [1, 1, 3, 5])]
        """
        return self.map(lambda x: (f(x), x)).groupByKey(numSplits)

    def pipe(self, command, env={}):
        """
        Return an RDD created by piping elements to a forked external process.

        >>> sc.parallelize([1, 2, 3]).pipe('cat').collect()
        ['1', '2', '3']
        """
        def func(iterator):
            pipe = Popen(shlex.split(command), env=env, stdin=PIPE, stdout=PIPE)
            def pipe_objs(out):
                for obj in iterator:
                    out.write(str(obj).rstrip('\n') + '\n')
                out.close()
            Thread(target=pipe_objs, args=[pipe.stdin]).start()
            return (x.rstrip('\n') for x in pipe.stdout)
        return self.mapPartitions(func)

    def foreach(self, f):
        """
        Applies a function to all elements of this RDD.

        >>> def f(x): print x
        >>> sc.parallelize([1, 2, 3, 4, 5]).foreach(f)
        """
        self.map(f).collect()  # Force evaluation

    def collect(self):
        """
        Return a list that contains all of the elements in this RDD.
        """
        picklesInJava = self._jrdd.collect().iterator()
        return list(self._collect_iterator_through_file(picklesInJava))

    def _collect_iterator_through_file(self, iterator):
        # Transferring lots of data through Py4J can be slow because
        # socket.readline() is inefficient.  Instead, we'll dump the data to a
        # file and read it back.
        tempFile = NamedTemporaryFile(delete=False)
        tempFile.close()
        def clean_up_file():
            try: os.unlink(tempFile.name)
            except: pass
        atexit.register(clean_up_file)
        self.ctx._writeIteratorToPickleFile(iterator, tempFile.name)
        # Read the data into Python and deserialize it:
        with open(tempFile.name, 'rb') as tempFile:
            for item in read_from_pickle_file(tempFile):
                yield item
        os.unlink(tempFile.name)

    def reduce(self, f):
        """
        Reduces the elements of this RDD using the specified associative binary
        operator.

        >>> from operator import add
        >>> sc.parallelize([1, 2, 3, 4, 5]).reduce(add)
        15
        >>> sc.parallelize((2 for _ in range(10))).map(lambda x: 1).cache().reduce(add)
        10
        """
        def func(iterator):
            acc = None
            for obj in iterator:
                if acc is None:
                    acc = obj
                else:
                    acc = f(obj, acc)
            if acc is not None:
                yield acc
        vals = self.mapPartitions(func).collect()
        return reduce(f, vals)

    def fold(self, zeroValue, op):
        """
        Aggregate the elements of each partition, and then the results for all
        the partitions, using a given associative function and a neutral "zero
        value."

        The function C{op(t1, t2)} is allowed to modify C{t1} and return it
        as its result value to avoid object allocation; however, it should not
        modify C{t2}.

        >>> from operator import add
        >>> sc.parallelize([1, 2, 3, 4, 5]).fold(0, add)
        15
        """
        def func(iterator):
            acc = zeroValue
            for obj in iterator:
                acc = op(obj, acc)
            yield acc
        vals = self.mapPartitions(func).collect()
        return reduce(op, vals, zeroValue)

    # TODO: aggregate

    def sum(self):
        """
        Add up the elements in this RDD.

        >>> sc.parallelize([1.0, 2.0, 3.0]).sum()
        6.0
        """
        return self.mapPartitions(lambda x: [sum(x)]).reduce(operator.add)

    def count(self):
        """
        Return the number of elements in this RDD.

        >>> sc.parallelize([2, 3, 4]).count()
        3
        """
        return self.mapPartitions(lambda i: [sum(1 for _ in i)]).sum()

    def countByValue(self):
        """
        Return the count of each unique value in this RDD as a dictionary of
        (value, count) pairs.

        >>> sorted(sc.parallelize([1, 2, 1, 2, 2], 2).countByValue().items())
        [(1, 2), (2, 3)]
        """
        def countPartition(iterator):
            counts = defaultdict(int)
            for obj in iterator:
                counts[obj] += 1
            yield counts
        def mergeMaps(m1, m2):
            for (k, v) in m2.iteritems():
                m1[k] += v
            return m1
        return self.mapPartitions(countPartition).reduce(mergeMaps)

    def take(self, num):
        """
        Take the first num elements of the RDD.

        This currently scans the partitions *one by one*, so it will be slow if
        a lot of partitions are required. In that case, use L{collect} to get
        the whole RDD instead.

        >>> sc.parallelize([2, 3, 4, 5, 6]).cache().take(2)
        [2, 3]
        >>> sc.parallelize([2, 3, 4, 5, 6]).take(10)
        [2, 3, 4, 5, 6]
        """
        items = []
        for partition in range(self._jrdd.splits().size()):
            iterator = self.ctx._takePartition(self._jrdd.rdd(), partition)
            items.extend(self._collect_iterator_through_file(iterator))
            if len(items) >= num:
                break
        return items[:num]

    def first(self):
        """
        Return the first element in this RDD.

        >>> sc.parallelize([2, 3, 4]).first()
        2
        """
        return self.take(1)[0]

    def saveAsTextFile(self, path):
        """
        Save this RDD as a text file, using string representations of elements.

        >>> tempFile = NamedTemporaryFile(delete=True)
        >>> tempFile.close()
        >>> sc.parallelize(range(10)).saveAsTextFile(tempFile.name)
        >>> from fileinput import input
        >>> from glob import glob
        >>> ''.join(input(glob(tempFile.name + "/part-0000*")))
        '0\\n1\\n2\\n3\\n4\\n5\\n6\\n7\\n8\\n9\\n'
        """
        def func(iterator):
            return (str(x).encode("utf-8") for x in iterator)
        keyed = PipelinedRDD(self, func)
        keyed._bypass_serializer = True
        keyed._jrdd.map(self.ctx.jvm.BytesToString()).saveAsTextFile(path)

    # Pair functions

    def collectAsMap(self):
        """
        Return the key-value pairs in this RDD to the master as a dictionary.

        >>> m = sc.parallelize([(1, 2), (3, 4)]).collectAsMap()
        >>> m[1]
        2
        >>> m[3]
        4
        """
        return dict(self.collect())

    def reduceByKey(self, func, numSplits=None):
        """
        Merge the values for each key using an associative reduce function.

        This will also perform the merging locally on each mapper before
        sending results to a reducer, similarly to a "combiner" in MapReduce.

        Output will be hash-partitioned with C{numSplits} splits, or the
        default parallelism level if C{numSplits} is not specified.

        >>> from operator import add
        >>> rdd = sc.parallelize([("a", 1), ("b", 1), ("a", 1)])
        >>> sorted(rdd.reduceByKey(add).collect())
        [('a', 2), ('b', 1)]
        """
        return self.combineByKey(lambda x: x, func, func, numSplits)

    def reduceByKeyLocally(self, func):
        """
        Merge the values for each key using an associative reduce function, but
        return the results immediately to the master as a dictionary.

        This will also perform the merging locally on each mapper before
        sending results to a reducer, similarly to a "combiner" in MapReduce.

        >>> from operator import add
        >>> rdd = sc.parallelize([("a", 1), ("b", 1), ("a", 1)])
        >>> sorted(rdd.reduceByKeyLocally(add).items())
        [('a', 2), ('b', 1)]
        """
        def reducePartition(iterator):
            m = {}
            for (k, v) in iterator:
                m[k] = v if k not in m else func(m[k], v)
            yield m
        def mergeMaps(m1, m2):
            for (k, v) in m2.iteritems():
                m1[k] = v if k not in m1 else func(m1[k], v)
            return m1
        return self.mapPartitions(reducePartition).reduce(mergeMaps)

    def countByKey(self):
        """
        Count the number of elements for each key, and return the result to the
        master as a dictionary.

        >>> rdd = sc.parallelize([("a", 1), ("b", 1), ("a", 1)])
        >>> sorted(rdd.countByKey().items())
        [('a', 2), ('b', 1)]
        """
        return self.map(lambda x: x[0]).countByValue()

    def join(self, other, numSplits=None):
        """
        Return an RDD containing all pairs of elements with matching keys in
        C{self} and C{other}.

        Each pair of elements will be returned as a (k, (v1, v2)) tuple, where
        (k, v1) is in C{self} and (k, v2) is in C{other}.

        Performs a hash join across the cluster.

        >>> x = sc.parallelize([("a", 1), ("b", 4)])
        >>> y = sc.parallelize([("a", 2), ("a", 3)])
        >>> sorted(x.join(y).collect())
        [('a', (1, 2)), ('a', (1, 3))]
        """
        return python_join(self, other, numSplits)

    def leftOuterJoin(self, other, numSplits=None):
        """
        Perform a left outer join of C{self} and C{other}.

        For each element (k, v) in C{self}, the resulting RDD will either
        contain all pairs (k, (v, w)) for w in C{other}, or the pair
        (k, (v, None)) if no elements in other have key k.

        Hash-partitions the resulting RDD into the given number of partitions.

        >>> x = sc.parallelize([("a", 1), ("b", 4)])
        >>> y = sc.parallelize([("a", 2)])
        >>> sorted(x.leftOuterJoin(y).collect())
        [('a', (1, 2)), ('b', (4, None))]
        """
        return python_left_outer_join(self, other, numSplits)

    def rightOuterJoin(self, other, numSplits=None):
        """
        Perform a right outer join of C{self} and C{other}.

        For each element (k, w) in C{other}, the resulting RDD will either
        contain all pairs (k, (v, w)) for v in this, or the pair (k, (None, w))
        if no elements in C{self} have key k.

        Hash-partitions the resulting RDD into the given number of partitions.

        >>> x = sc.parallelize([("a", 1), ("b", 4)])
        >>> y = sc.parallelize([("a", 2)])
        >>> sorted(y.rightOuterJoin(x).collect())
        [('a', (2, 1)), ('b', (None, 4))]
        """
        return python_right_outer_join(self, other, numSplits)

    # TODO: add option to control map-side combining
    def partitionBy(self, numSplits, hashFunc=hash):
        """
        Return a copy of the RDD partitioned using the specified partitioner.

        >>> pairs = sc.parallelize([1, 2, 3, 4, 2, 4, 1]).map(lambda x: (x, x))
        >>> sets = pairs.partitionBy(2).glom().collect()
        >>> set(sets[0]).intersection(set(sets[1]))
        set([])
        """
        if numSplits is None:
            numSplits = self.ctx.defaultParallelism
        # Transferring O(n) objects to Java is too expensive.  Instead, we'll
        # form the hash buckets in Python, transferring O(numSplits) objects
        # to Java.  Each object is a (splitNumber, [objects]) pair.
        def add_shuffle_key(iterator):
            buckets = defaultdict(list)
            for (k, v) in iterator:
                buckets[hashFunc(k) % numSplits].append((k, v))
            for (split, items) in buckets.iteritems():
                yield str(split)
                yield dump_pickle(Batch(items))
        keyed = PipelinedRDD(self, add_shuffle_key)
        keyed._bypass_serializer = True
        pairRDD = self.ctx.jvm.PairwiseRDD(keyed._jrdd.rdd()).asJavaPairRDD()
        partitioner = self.ctx.jvm.spark.api.python.PythonPartitioner(numSplits)
        jrdd = pairRDD.partitionBy(partitioner)
        jrdd = jrdd.map(self.ctx.jvm.ExtractValue())
        return RDD(jrdd, self.ctx)

    # TODO: add control over map-side aggregation
    def combineByKey(self, createCombiner, mergeValue, mergeCombiners,
                     numSplits=None):
        """
        Generic function to combine the elements for each key using a custom
        set of aggregation functions.

        Turns an RDD[(K, V)] into a result of type RDD[(K, C)], for a "combined
        type" C.  Note that V and C can be different -- for example, one might
        group an RDD of type (Int, Int) into an RDD of type (Int, List[Int]).

        Users provide three functions:

            - C{createCombiner}, which turns a V into a C (e.g., creates
              a one-element list)
            - C{mergeValue}, to merge a V into a C (e.g., adds it to the end of
              a list)
            - C{mergeCombiners}, to combine two C's into a single one.

        In addition, users can control the partitioning of the output RDD.

        >>> x = sc.parallelize([("a", 1), ("b", 1), ("a", 1)])
        >>> def f(x): return x
        >>> def add(a, b): return a + str(b)
        >>> sorted(x.combineByKey(str, add, add).collect())
        [('a', '11'), ('b', '1')]
        """
        if numSplits is None:
            numSplits = self.ctx.defaultParallelism
        def combineLocally(iterator):
            combiners = {}
            for (k, v) in iterator:
                if k not in combiners:
                    combiners[k] = createCombiner(v)
                else:
                    combiners[k] = mergeValue(combiners[k], v)
            return combiners.iteritems()
        locally_combined = self.mapPartitions(combineLocally)
        shuffled = locally_combined.partitionBy(numSplits)
        def _mergeCombiners(iterator):
            combiners = {}
            for (k, v) in iterator:
                if not k in combiners:
                    combiners[k] = v
                else:
                    combiners[k] = mergeCombiners(combiners[k], v)
            return combiners.iteritems()
        return shuffled.mapPartitions(_mergeCombiners)

    # TODO: support variant with custom partitioner
    def groupByKey(self, numSplits=None):
        """
        Group the values for each key in the RDD into a single sequence.
        Hash-partitions the resulting RDD with into numSplits partitions.

        >>> x = sc.parallelize([("a", 1), ("b", 1), ("a", 1)])
        >>> sorted(x.groupByKey().collect())
        [('a', [1, 1]), ('b', [1])]
        """

        def createCombiner(x):
            return [x]

        def mergeValue(xs, x):
            xs.append(x)
            return xs

        def mergeCombiners(a, b):
            return a + b

        return self.combineByKey(createCombiner, mergeValue, mergeCombiners,
                numSplits)

    # TODO: add tests
    def flatMapValues(self, f):
        """
        Pass each value in the key-value pair RDD through a flatMap function
        without changing the keys; this also retains the original RDD's
        partitioning.
        """
        flat_map_fn = lambda (k, v): ((k, x) for x in f(v))
        return self.flatMap(flat_map_fn, preservesPartitioning=True)

    def mapValues(self, f):
        """
        Pass each value in the key-value pair RDD through a map function
        without changing the keys; this also retains the original RDD's
        partitioning.
        """
        map_values_fn = lambda (k, v): (k, f(v))
        return self.map(map_values_fn, preservesPartitioning=True)

    # TODO: support varargs cogroup of several RDDs.
    def groupWith(self, other):
        """
        Alias for cogroup.
        """
        return self.cogroup(other)

    # TODO: add variant with custom parittioner
    def cogroup(self, other, numSplits=None):
        """
        For each key k in C{self} or C{other}, return a resulting RDD that
        contains a tuple with the list of values for that key in C{self} as well
        as C{other}.

        >>> x = sc.parallelize([("a", 1), ("b", 4)])
        >>> y = sc.parallelize([("a", 2)])
        >>> sorted(x.cogroup(y).collect())
        [('a', ([1], [2])), ('b', ([4], []))]
        """
        return python_cogroup(self, other, numSplits)

    # TODO: `lookup` is disabled because we can't make direct comparisons based
    # on the key; we need to compare the hash of the key to the hash of the
    # keys in the pairs.  This could be an expensive operation, since those
    # hashes aren't retained.


class PipelinedRDD(RDD):
    """
    Pipelined maps:
    >>> rdd = sc.parallelize([1, 2, 3, 4])
    >>> rdd.map(lambda x: 2 * x).cache().map(lambda x: 2 * x).collect()
    [4, 8, 12, 16]
    >>> rdd.map(lambda x: 2 * x).map(lambda x: 2 * x).collect()
    [4, 8, 12, 16]

    Pipelined reduces:
    >>> from operator import add
    >>> rdd.map(lambda x: 2 * x).reduce(add)
    20
    >>> rdd.flatMap(lambda x: [x, x]).reduce(add)
    20
    """
    def __init__(self, prev, func, preservesPartitioning=False):
        if isinstance(prev, PipelinedRDD) and not prev.is_cached:
            prev_func = prev.func
            def pipeline_func(iterator):
                return func(prev_func(iterator))
            self.func = pipeline_func
            self.preservesPartitioning = \
                prev.preservesPartitioning and preservesPartitioning
            self._prev_jrdd = prev._prev_jrdd
        else:
            self.func = func
            self.preservesPartitioning = preservesPartitioning
            self._prev_jrdd = prev._jrdd
        self.is_cached = False
        self.ctx = prev.ctx
        self.prev = prev
        self._jrdd_val = None
        self._bypass_serializer = False

    @property
    def _jrdd(self):
        if self._jrdd_val:
            return self._jrdd_val
        func = self.func
        if not self._bypass_serializer and self.ctx.batchSize != 1:
            oldfunc = self.func
            batchSize = self.ctx.batchSize
            def batched_func(iterator):
                return batched(oldfunc(iterator), batchSize)
            func = batched_func
        cmds = [func, self._bypass_serializer]
        pipe_command = ' '.join(b64enc(cloudpickle.dumps(f)) for f in cmds)
        broadcast_vars = ListConverter().convert(
            [x._jbroadcast for x in self.ctx._pickled_broadcast_vars],
            self.ctx.gateway._gateway_client)
        self.ctx._pickled_broadcast_vars.clear()
        class_manifest = self._prev_jrdd.classManifest()
        env = copy.copy(self.ctx.environment)
        env['PYTHONPATH'] = os.environ.get("PYTHONPATH", "")
        env = MapConverter().convert(env, self.ctx.gateway._gateway_client)
        python_rdd = self.ctx.jvm.PythonRDD(self._prev_jrdd.rdd(),
            pipe_command, env, self.preservesPartitioning, self.ctx.pythonExec,
            broadcast_vars, class_manifest)
        self._jrdd_val = python_rdd.asJavaRDD()
        return self._jrdd_val


def _test():
    import doctest
    from pyspark.context import SparkContext
    globs = globals().copy()
    # The small batch size here ensures that we see multiple batches,
    # even in these small test examples:
    globs['sc'] = SparkContext('local[4]', 'PythonTest', batchSize=2)
    doctest.testmod(globs=globs)
    globs['sc'].stop()


if __name__ == "__main__":
    _test()
