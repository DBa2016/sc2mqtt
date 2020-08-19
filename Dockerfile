FROM python:alpine

RUN apk add libxml2-dev && apk add libxslt-dev && apk add build-base && apk add git

WORKDIR /usr/src/app

RUN git clone https://github.com/zaubererty/sc2mqtt.git .

RUN pip install --no-cache-dir -r requirements.txt

CMD [ "python", "./sc2mqtt.py" ]