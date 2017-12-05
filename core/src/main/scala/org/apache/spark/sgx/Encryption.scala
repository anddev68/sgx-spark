package org.apache.spark.sgx

import java.util.Base64
import org.apache.spark.internal.Logging

  /*
   * TODO: Encryption/Decryption are dummy operations.
   */

trait Encrypted extends Serializable {
	def decrypt[U]: U
}

trait Encryptable extends Serializable {
	def encrypt: Encrypted
}

private class EncryptedObj[T](cipher: T, dec: T => Any) extends Encrypted {
	def decrypt[U]: U = {
		if (SgxSettings.IS_ENCLAVE) dec(cipher).asInstanceOf[U]
		else throw new RuntimeException("Must not decrypt outside of enclave")
	}
}

object Decrypt extends Logging {
	def apply[U](cipher: Any): U = {
		val x = if (cipher == null) null.asInstanceOf[U]
		else cipher match {
			case e: Encrypted => e.decrypt[U]
			case p: Any => p.asInstanceOf[U]
		}
		logDebug("Decrypting: " + cipher + (if (cipher != null) " (" +  cipher.getClass().getName + ")" else "") + " to " + x)
		x
	}
}

object Encrypt {
	def apply(plain: Any): Encrypted = Base64StringEncrypt(plain)
}

private object Base64StringEncrypt extends Logging {
	def apply(plain: Any): Encrypted = {
		val x = if (plain == null) null.asInstanceOf[Encrypted]
		else plain match {
			case e: Encrypted => e
			case e: Encryptable => e.encrypt
			case t: Tuple2[_,_] => new EncryptedTuple2(Encrypt(t._1), Encrypt(t._2))
			case p: Any =>
				new EncryptedObj[String](
					Base64.getEncoder.encodeToString(Serialization.serialize(plain)),
					(x: String) => Serialization.deserialize(Base64.getDecoder.decode(x))
				)
		}
		logDebug("Encrypting: " + plain + (if (plain !=null) " (" + plain.getClass().getName + ")" else "") + " to " + x)
		x
	}
}

class EncryptedTuple2[T1,T2](t1: Encrypted, t2: Encrypted) extends Product2[T1,T2] with Encrypted {
	def decrypt[U]: U = {
		if (SgxSettings.IS_ENCLAVE) (t1.decrypt[T1],t2.decrypt[T2]).asInstanceOf[U]
		else throw new RuntimeException("Must not decrypt outside of enclave")
	}

	def _1 = t1.decrypt[T1]
	def _2 = t2.decrypt[T2]
	def canEqual(that: Any) = decrypt[Product2[T1,T2]].canEqual(that)
}

class EncryptedBoolean(b: Encrypted) extends Encrypted {
	def decrypt[U] = b.decrypt[U]
}

object EncryptionUtils {
	implicit class BooleanEncryption(val b: Boolean) extends Encryptable {
		def encrypt = new EncryptedBoolean(Encrypt(b))
	}
}
