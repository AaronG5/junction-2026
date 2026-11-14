i want this part of the project to have these functions (these all come into the launch script, which wors for launching the program the first time and then all times after it.):

1. detect computer hardware capabilities.
2. have a dataset of available google gemma models.
3. select which model would fit best on the hardware.
4. start a curl script to download the model with light traffic (curl is limited for hugging face so it should be around 1MB/s, which puts light load on Junction's wifi). It should be downloaded to project/models/ directory, where project is the current path of the terminal (you get the idea). The model should be downloaded to it's own directory, where the directory is the name of the model file? 
5. clone and compile llama.cpp with llama swap, generate a llama.swap config for the model. This should work on all devices - windows and linux included.
6. Download an embedding model to /models/embeddings. The embedding model will be set later, probably one of google's multilingual ones.
7. init/start docker with Qdrant.
8. init/start docker with mysql.
9. start llama swap to be open as an openaiapi. It will be called with specific functions later. (calling localhost)

more steps will be added i the process.
