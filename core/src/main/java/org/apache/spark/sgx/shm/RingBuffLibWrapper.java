package org.apache.spark.sgx.shm;

public class RingBuffLibWrapper {

	static {
		System.loadLibrary("ringbuff");
	}

	public static native long[] init_shm(String file, long size);

	public static native boolean write_msg(long handle, byte[] msg);

	public static native byte[] read_msg(long handle);
}