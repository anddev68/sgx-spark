package org.apache.spark.sgx;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.Callable;
import java.util.concurrent.LinkedBlockingQueue;

/**
 * This class is to be used by the enclave to communicate with the outside.
 * 
 * @author Florian Kelbert
 *
 */
public final class ShmCommunicationManager<T> implements Callable<T> {
	private RingBuff writeBuff;
	private RingBuff readBuff;

	private final Object lockWriteBuff = new Object();
	private final Object lockReadBuff = new Object();
	private final Object lockInboxes = new Object();

	private Map<Long, BlockingQueue<Object>> inboxes = new HashMap<>();
	private BlockingQueue<ShmCommunicator> accepted = new LinkedBlockingQueue<>();
	
	private long inboxCtr = 1;
	
	private static ShmCommunicationManager<?> _instance = null;

	private ShmCommunicationManager(String file, int size) {
		long[] handles = RingBuffLibWrapper.init_shm(file, size);
		this.readBuff = new RingBuff(handles[0], true);
		this.writeBuff = new RingBuff(handles[1], true);
	}

	private ShmCommunicationManager(long writeBuff, long readBuff) {
		this.writeBuff = new RingBuff(writeBuff, true);
		this.readBuff = new RingBuff(readBuff, true);
	}
	
	@SuppressWarnings("unchecked")
	public static <T> ShmCommunicationManager<T> create(String file, int size) {
		if (_instance == null) {
			_instance = new ShmCommunicationManager<T>(file, size);
		}
		return (ShmCommunicationManager<T>) _instance;
	}
	
	@SuppressWarnings("unchecked")
	public static <T> ShmCommunicationManager<T> create(long writeBuff, long readBuff) {
		if (_instance == null) {
			_instance = new ShmCommunicationManager<T>(writeBuff, readBuff);
		}
		return (ShmCommunicationManager<T>) _instance;
	}
	
	@SuppressWarnings("unchecked")
	public static <T> ShmCommunicationManager<T> get() {
		if (_instance == null) {
			throw new RuntimeException(_instance.getClass().getSimpleName() + " was not instantiated.");
		}
		return (ShmCommunicationManager<T>) _instance;
	}

	public ShmCommunicator newShmCommunicator() {
		BlockingQueue<Object> inbox = new LinkedBlockingQueue<>();
		long myport;
		synchronized (lockInboxes) {
			myport = inboxCtr++;
			inboxes.put(myport, inbox);
		}

		return new ShmCommunicator(myport, inbox, this);
	}

	public ShmCommunicator accept() {
		ShmCommunicator result = null;
		do {
			try {
				result = accepted.take();
			} catch (InterruptedException e) {
				result = null;
				e.printStackTrace();
			}
		} while (result == null);
		return result;
	}

	/**
	 * Writes to the outside.
	 * 
	 * @param o the object to write
	 * @return whether the write was successful
	 */
	boolean write(Object o, long theirPort) {
		return write(new ShmMessage(EShmMessageType.REGULAR, o, theirPort));
	}

	boolean write(ShmMessage m) {
		synchronized (lockWriteBuff) {
			return writeBuff.write(m);
		}
	}
	
	void close(ShmCommunicator com) {
		inboxes.remove(com.getMyPort());
	}

	@Override
	public T call() throws Exception {
		ShmMessage msg = null;
		while (true) {
			synchronized (lockReadBuff) {
				msg = ((ShmMessage) readBuff.read());
			}

			if (msg.getPort() == 0) {
				switch (msg.getType()) {
				case NEW_CONNECTION:
					BlockingQueue<Object> inbox = new LinkedBlockingQueue<>();
					long myport;
					synchronized (lockInboxes) {
						myport = inboxCtr++;
						inboxes.put(myport, inbox);
					}
					accepted.put(new ShmCommunicator(myport, (long) msg.getMsg(), inbox, this));
					write(new ShmMessage(EShmMessageType.ACCEPTED_CONNECTION, myport, (int) msg.getMsg()));
					break;

				case CLOSE_CONNECTION:
					break;

				case REGULAR:
					break;

				default:
					break;
				}
			} else {
				BlockingQueue<Object> inbox;
				synchronized (lockInboxes) {
					inbox = inboxes.get(msg.getPort());
				}
				inbox.add(msg.getMsg());
			}
		}
	}
}