
import asyncio
import warera


async def main():
    player_id = "69f9a85dd04c814cdd802d0b"

    try:
        user = await warera.user.get_by_id(player_id)

        print("SUCCESS! WarEra profile found.")
        print(user.model_dump_json(indent=2))

    except Exception as error:
        print("WarEra lookup failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Details: {error}")


if __name__ == "__main__":
    asyncio.run(main())
