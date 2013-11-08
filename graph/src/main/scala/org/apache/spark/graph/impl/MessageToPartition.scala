package org.apache.spark.graph.impl

import org.apache.spark.Partitioner
import org.apache.spark.graph.{Pid, Vid}
import org.apache.spark.rdd.{ShuffledRDD, RDD}


class VertexMessage[@specialized(Int, Long, Double, Boolean/*, AnyRef*/) T](
    @transient var partition: Pid,
    var vid: Vid,
    var data: T)
  extends Product2[Pid, (Vid, T)] {

  override def _1 = partition

  override def _2 = (vid, data)

  override def canEqual(that: Any): Boolean = that.isInstanceOf[VertexMessage[_]]
}


/**
 * A message used to send a specific value to a partition.
 * @param partition index of the target partition.
 * @param data value to send
 */
class MessageToPartition[@specialized(Int, Long, Double, Char, Boolean/*, AnyRef*/) T](
    @transient var partition: Pid,
    var data: T)
  extends Product2[Pid, T] {

  override def _1 = partition

  override def _2 = data

  override def canEqual(that: Any): Boolean = that.isInstanceOf[MessageToPartition[_]]
}

/**
 * Companion object for MessageToPartition.
 */
object MessageToPartition {
  def apply[T](partition: Pid, value: T) = new MessageToPartition(partition, value)
}


class VertexMessageRDDFunctions[T: ClassManifest](self: RDD[VertexMessage[T]]) {
  def partitionBy(partitioner: Partitioner): RDD[VertexMessage[T]] = {
    val rdd = new ShuffledRDD[Pid, (Vid, T), VertexMessage[T]](self, partitioner)

    // Set a custom serializer if the data is of int or double type.
    if (classManifest[T] == ClassManifest.Int) {
      rdd.setSerializer(classOf[IntVertexMessageSerializer].getName)
    } else if (classManifest[T] == ClassManifest.Double) {
      rdd.setSerializer(classOf[DoubleVertexMessageSerializer].getName)
    }
    rdd
  }
}


class MessageToPartitionRDDFunctions[T: ClassManifest](self: RDD[MessageToPartition[T]]) {

  /**
   * Return a copy of the RDD partitioned using the specified partitioner.
   */
  def partitionBy(partitioner: Partitioner): RDD[MessageToPartition[T]] = {
    new ShuffledRDD[Pid, T, MessageToPartition[T]](self, partitioner)
  }

}


object MessageToPartitionRDDFunctions {
  implicit def rdd2PartitionRDDFunctions[T: ClassManifest](rdd: RDD[MessageToPartition[T]]) = {
    new MessageToPartitionRDDFunctions(rdd)
  }

  implicit def rdd2vertexMessageRDDFunctions[T: ClassManifest](rdd: RDD[VertexMessage[T]]) = {
    new VertexMessageRDDFunctions(rdd)
  }
}
