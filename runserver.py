from app import create_app

app = create_app()


# mian
if __name__ == "__main__":
    app.run(debug=True, port=8888, host='10.0.0.4')
