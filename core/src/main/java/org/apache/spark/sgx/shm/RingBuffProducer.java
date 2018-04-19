package org.apache.spark.sgx.shm;

import org.apache.spark.sgx.Serialization;
import org.apache.spark.sgx.data.AlignedMappedDataBuffer;
import org.apache.spark.sgx.data.MappedDataBuffer;

class RingBuffProducer {
	private AlignedMappedDataBuffer buffer;
	
	RingBuffProducer(MappedDataBuffer buffer) {
		this.buffer = new AlignedMappedDataBuffer(buffer);
		System.out.println("Creating " + this);
	}

	/*
	 * TODO:
	 * - Align with cache line 64 Byte
	 * - Deserialization: do not copy first into local, deserialize directly from shared memory
	 * - Wrapping at end of buffer
	 * - Use System.arracopy
	 * - madvise: do not page out
	 */
	
	boolean write(Object o) {
		boolean exception = false;
		boolean success = false;
		
		do {
			try {				
				byte[] bytes = Serialization.serialize(o);
				int pos = buffer.position();
				System.out.println("Waiting to write object " + o);
				buffer.waitUntil(0);
				buffer.put(bytes);
				buffer.putInt(pos, bytes.length);
				success = true;
				System.out.println("Done writing object " + o + " of length " + bytes.length);
			} catch (Exception e) {
				e.printStackTrace();
				exception = true;
			}
		} while (!success && !exception);
		return true;
	}
	
	@Override
	public String toString() {
		return this.getClass().getSimpleName() + "(buffer=" + buffer + ")";
	}
}
