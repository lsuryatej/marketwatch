FROM node:20-alpine as build

WORKDIR /app

COPY package.json /app/package.json
COPY package-lock.json /app/package-lock.json || true

RUN npm install

COPY . /app

RUN npm run build

EXPOSE 3000

CMD ["npm", "start"]