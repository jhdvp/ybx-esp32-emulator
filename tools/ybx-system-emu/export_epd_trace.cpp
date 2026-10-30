// Real FreeInk SSD1677 driver → recorded SPI command/data trace.
// Host stubs are provided by the SDK's existing test/host/pro_stubs.
#include <cstdint>
#include <fstream>
#include <vector>
#include <string>
#include <stdexcept>
#include "src/driver/Ssd1677Driver.h"
#include "src/driver/Uc8253X3Driver.h"
#include <BoardConfig.h>

int main(int argc,char**argv) {
  if(argc != 3)return 2;
  const std::string target=argv[2];
  if(target!="ssd1677" && target!="uc8253")return 2;
  const char magic[6]={'Y','E','P','D','1','\0'};
  std::ofstream file(argv[1],std::ios::binary);
  if(!file)return 3;
  file.write(magic,6);
  const bool x3=target=="uc8253";
  const int width=x3?792:800, height=x3?528:480;
  BoardConfig::ACTIVE.displayWidth=width;
  BoardConfig::ACTIVE.displayHeight=height;
  BoardConfig::ACTIVE.board=x3?BoardConfig::Board::XteinkX3:BoardConfig::Board::XteinkX4;
  std::vector<uint8_t> framebuffer(width/8*height,0xff);
  // Distinctive nonuniform test image: entire first scanline black,
  // one pixel in second row black, remaining lines white.
  for(int x=0;x<width/8;x++)framebuffer[x]=0;
  framebuffer[width/8]=0x7f;
  freeink::EpdBus bus;
  if(x3) {
    freeink::Uc8253X3Driver panel;
    panel.begin(bus);
    panel.display(bus,framebuffer.data(),nullptr,freeink::RefreshMode::Full,false);
  } else {
    freeink::Ssd1677Driver panel;
    panel.begin(bus);
    panel.display(bus,framebuffer.data(),nullptr,freeink::RefreshMode::Full,false);
  }
  if(bus.writes.empty())throw std::runtime_error("Driver recorded no SPI commands");
  for(const auto& write:bus.writes) {
    uint8_t cmd=write.command;
    const uint32_t n=static_cast<uint32_t>(write.bytes.size());
    if(n>128*1024)return 4;
    const uint8_t len[4]={uint8_t(n),uint8_t(n>>8),uint8_t(n>>16),uint8_t(n>>24)};
    file.write(reinterpret_cast<char*>(&cmd),1);
    file.write(reinterpret_cast<const char*>(len),4);
    if(n)file.write(reinterpret_cast<const char*>(write.bytes.data()),n);
  }
  return file.good()?0:5;
}
