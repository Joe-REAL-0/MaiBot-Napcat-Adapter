import asyncio
import sys
import json
import http
import websockets as Server
from src.logger import logger
from src.recv_handler.message_handler import message_handler
from src.recv_handler.meta_event_handler import meta_event_handler
from src.recv_handler.notice_handler import notice_handler
from src.recv_handler.message_sending import message_send_instance
from src.send_handler.nc_sending import nc_message_sender
from src.config import global_config
from src.mmc_com_layer import mmc_start_com, mmc_stop_com, router
from src.response_pool import put_response, check_timeout_response

message_queue = asyncio.Queue()


async def message_recv(server_connection: Server.ServerConnection):
    await message_handler.set_server_connection(server_connection)
    asyncio.create_task(notice_handler.set_server_connection(server_connection))
    await nc_message_sender.set_server_connection(server_connection)
    async for raw_message in server_connection:
        try:
            logger.debug(f"{raw_message[:1500]}..." if (len(raw_message) > 1500) else raw_message)
            decoded_raw_message: dict = json.loads(raw_message)
        except Exception as e:
            logger.error(f"JSON解析失败: {e}")
            continue
        try:
            post_type = decoded_raw_message.get("post_type")
            if post_type in ["meta_event", "message", "notice"]:
                await message_queue.put(decoded_raw_message)
            elif post_type is None:
                await put_response(decoded_raw_message)
            else:
                logger.warning(f"未知的post_type: {post_type}")
        except Exception as e:
            logger.exception(f"消息入队或响应处理异常: {e}")


async def message_process():
    while True:
        message = await message_queue.get()
        try:
            post_type = message.get("post_type")
            if post_type == "message":
                await message_handler.handle_raw_message(message)
            elif post_type == "meta_event":
                await meta_event_handler.handle_meta_event(message)
            elif post_type == "notice":
                await notice_handler.handle_notice(message)
            else:
                logger.warning(f"未知的post_type: {post_type}")
        except Exception as e:
            logger.exception(f"消息处理异常: {e}")
        finally:
            message_queue.task_done()
        await asyncio.sleep(0.05)


async def main():
    message_send_instance.maibot_router = router
    _ = await asyncio.gather(napcat_server(), mmc_start_com(), message_process(), check_timeout_response())

def check_napcat_server_token(conn, request):
    token = global_config.napcat_server.token
    if not token or token.strip() == "":
        return None
    auth_header = request.headers.get("Authorization")
    if auth_header != f"Bearer {token}":
        return Server.Response(
            status=http.HTTPStatus.UNAUTHORIZED,
            headers=Server.Headers([("Content-Type", "text/plain")]),
            body=b"Unauthorized\n"
        )
    return None

async def napcat_server():
    logger.info("正在启动adapter...")
    async with Server.serve(message_recv, global_config.napcat_server.host, global_config.napcat_server.port, max_size=2**26, process_request=check_napcat_server_token) as server:
        logger.info(
            f"Adapter已启动，监听地址: ws://{global_config.napcat_server.host}:{global_config.napcat_server.port}"
        )
        await server.serve_forever()


async def graceful_shutdown():
    try:
        logger.info("正在关闭adapter...")
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
        await mmc_stop_com()  # 后置避免神秘exception
        logger.info("Adapter已成功关闭")
    except Exception as e:
        logger.error(f"Adapter关闭中出现错误: {e}")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.warning("收到中断信号，正在优雅关闭...")
        loop.run_until_complete(graceful_shutdown())
    except Exception as e:
        logger.exception(f"主程序异常: {str(e)}")
        # 尝试优雅关闭，取消仍在运行的任务，避免残留pending任务和未关闭的会话
        try:
            loop.run_until_complete(graceful_shutdown())
        except Exception as ge:
            logger.error(f"优雅关闭过程中出现错误: {ge}")
        sys.exit(1)
    finally:
        if loop and not loop.is_closed():
            loop.close()
        sys.exit(0)
