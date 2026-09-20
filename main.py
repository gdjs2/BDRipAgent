def main():
    import uvicorn

    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
