package picker.infra.redis

import io.lettuce.core.ExperimentalLettuceCoroutinesApi
import io.lettuce.core.RedisClient
import io.lettuce.core.api.StatefulRedisConnection
import io.lettuce.core.api.coroutines
import io.lettuce.core.api.coroutines.RedisCoroutinesCommands

/** One shared Lettuce connection (thread-safe, multiplexed). */
@OptIn(ExperimentalLettuceCoroutinesApi::class)
class Redis(url: String) : AutoCloseable {
    private val client: RedisClient = RedisClient.create(url)
    private val connection: StatefulRedisConnection<String, String> = client.connect()
    val commands: RedisCoroutinesCommands<String, String> = connection.coroutines()

    override fun close() {
        connection.close()
        client.shutdown()
    }
}
