package com.aurora.wechatrelay.probe

import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

object AesGcmVectors {
    fun decryptUrlSafe(key: String, iv: String, aad: String, ciphertext: String): String {
        val decoder = Base64.getUrlDecoder()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(decoder.decode(key), "AES"), GCMParameterSpec(128, decoder.decode(iv)))
        cipher.updateAAD(aad.toByteArray(Charsets.UTF_8))
        return cipher.doFinal(decoder.decode(ciphertext)).toString(Charsets.UTF_8)
    }
}
