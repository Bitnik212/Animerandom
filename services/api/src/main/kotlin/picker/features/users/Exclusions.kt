package picker.features.users

import io.lettuce.core.ExperimentalLettuceCoroutinesApi
import io.lettuce.core.ScriptOutputType
import kotlinx.coroutines.flow.toList
import picker.infra.redis.Keys
import picker.infra.redis.Redis
import java.util.UUID

/**
 * `user:{id}:excluded` (infra/README.md "Redis"): watched and not-interested anime IDs, used to
 * hide them from random picks and search. Rebuilt from Postgres on a miss and kept for 24 hours;
 * list changes update it in place through [add] / [remove].
 *
 * Redis can't hold an empty set, so the set always contains [SENTINEL] (catalog IDs start at 1).
 * That keeps "no exclusions" distinguishable from "not built yet".
 */
@OptIn(ExperimentalLettuceCoroutinesApi::class)
class Exclusions(private val redis: Redis, private val users: UserRepository) {
    /**
     * Makes sure the set exists and returns its key, for use in `SDIFFSTORE`. The TTL is set only
     * when the set is built (atomically with its members), never on reads, so every set really is
     * rebuilt from Postgres at least once a day, even for users who are active all the time.
     */
    suspend fun ensure(userId: UUID): String {
        val key = Keys.userExcluded(userId)
        if (redis.commands.exists(key) == 0L) {
            val ids = users.excludedIds(userId).map { it.toString() } + SENTINEL
            redis.commands.eval<Long>(
                BUILD_IF_MISSING,
                ScriptOutputType.INTEGER,
                arrayOf(key),
                TTL_SECONDS.toString(),
                *ids.toTypedArray(),
            )
        }
        return key
    }

    suspend fun ids(userId: UUID): Set<Long> =
        redis.commands
            .smembers(ensure(userId))
            .toList()
            .map { it.toLong() }
            .toSet() - SENTINEL.toLong()

    /**
     * Updates an existing set in place; a missing set is left for the next rebuild. Atomic, so a
     * key expiring mid-update can't leave behind a partial set without TTL.
     */
    suspend fun add(userId: UUID, animeId: Long) {
        redis.commands.eval<Long>(
            ADD_IF_EXISTS,
            ScriptOutputType.INTEGER,
            arrayOf(Keys.userExcluded(userId)),
            animeId.toString(),
        )
    }

    suspend fun remove(userId: UUID, animeId: Long) {
        redis.commands.srem(Keys.userExcluded(userId), animeId.toString())
    }

    companion object {
        const val SENTINEL = "0"
        const val TTL_SECONDS = 24 * 3600L
        private const val BUILD_IF_MISSING =
            "if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end " +
                "redis.call('SADD', KEYS[1], unpack(ARGV, 2)) redis.call('EXPIRE', KEYS[1], ARGV[1]) return 1"
        private const val ADD_IF_EXISTS =
            "if redis.call('EXISTS', KEYS[1]) == 1 then return redis.call('SADD', KEYS[1], ARGV[1]) end return 0"
    }
}
