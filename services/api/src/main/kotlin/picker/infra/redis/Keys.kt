package picker.infra.redis

import java.security.MessageDigest
import java.util.UUID

/**
 * Every Redis key this service reads or writes. Mirrors the key table in infra/README.md;
 * no other file builds key strings.
 */
object Keys {
    // Written by the ingest worker; read-only here.
    const val POOL_ALL = "pool:all"

    fun poolGenre(slug: String) = "pool:genre:$slug"

    fun poolFormat(format: String) = "pool:format:$format"

    fun poolScore(bucket: Int) = "pool:score:${bucket}plus"

    fun poolDecade(decade: String) = "pool:decade:$decade"

    fun poolLength(bucket: String) = "pool:length:$bucket"

    // Owned by this service.
    fun userExcluded(userId: UUID) = "user:$userId:excluded"

    fun userPattern(userId: UUID) = "user:$userId:*"

    fun animeCard(animeId: Long, localeKey: String) = "cache:anime:$animeId:$localeKey"

    fun randomScratch(id: UUID = UUID.randomUUID()) = "tmp:rand:$id"

    fun authRateIp(ip: String) = "ratelimit:auth:ip:$ip"

    fun authRateEmail(email: String) = "ratelimit:auth:email:${sha256(email.trim().lowercase())}"

    private fun sha256(value: String): String =
        MessageDigest.getInstance("SHA-256").digest(value.toByteArray()).joinToString("") { "%02x".format(it) }
}
