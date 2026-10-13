// Genuine LCZero backend: delegate policy to ChessGPT and value to
// Stockfish 16 classical eval. Both are served by Python over localhost.
// Linux bridge prototype; Lc0 performs its ORIGINAL search.
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>
#include "neural/backend.h"
#include "neural/register.h"
#include "chess/position.h"

namespace lczero {
namespace {

void SendAll(int fd, const std::string& s) {
  size_t n=0;
  while (n<s.size()) {
    auto k=send(fd,s.data()+n,s.size()-n,MSG_NOSIGNAL);
    if (k<=0) throw std::runtime_error("lc0 ChessGPT bridge send failed");
    n+=static_cast<size_t>(k);
  }
}
std::string ReadLine(int fd) {
  std::string res;
  for (;;) {
    char c=0;
    auto n=recv(fd,&c,1,0);
    if (n<=0) throw std::runtime_error("lc0 ChessGPT bridge receive failed");
    if (c=='\n') return res;
    res+=c;
    if (res.size()>1000000) throw std::runtime_error("oversized bridge response");
  }
}
struct Pending {
  std::vector<std::string> fens;
  std::vector<std::string> moves;
  EvalResultPtr out;
};

class BridgeComputation final : public BackendComputation {
 public:
  size_t UsedBatchSize() const override { return work_.size(); }
  AddInputResult AddInput(const EvalPosition& pos, EvalResultPtr out) override {
    Pending w;
    w.out=out;
    for (const auto& p: pos.pos) w.fens.push_back(PositionToFen(p));
    const bool black=pos.pos.back().IsBlackToMove();
    for (const auto& m:pos.legal_moves) {
      Move move=m;
      if (black) move.Flip();
      w.moves.push_back(move.ToString(false));
    }
    work_.push_back(std::move(w));
    return ENQUEUED_FOR_EVAL;
  }
  void ComputeBlocking() override {
    if (work_.empty()) return;
    int fd=socket(AF_INET,SOCK_STREAM,0);
    if (fd<0) throw std::runtime_error("Lc0 bridge socket unavailable");
    try {
      struct timeval tv{30,0};
      setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&tv,sizeof(tv));
      setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&tv,sizeof(tv));
      sockaddr_in addr{};
      addr.sin_family=AF_INET;
      int port=19217;
      if (auto env=std::getenv("CHESSGPT_LC0_PORT")) port=std::stoi(env);
      addr.sin_port=htons(static_cast<unsigned short>(port));
      addr.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
      if(connect(fd,reinterpret_cast<sockaddr*>(&addr),sizeof(addr))!=0)
        throw std::runtime_error("Lc0 bridge service connection refused");
      std::ostringstream req;
      req << work_.size() << '\n';
      for (auto& w:work_) {
        req << w.fens.size() << '\n';
        for (const auto& f:w.fens) req << f << '\n';
        req << w.moves.size() << '\n';
        for (const auto& m:w.moves) req << m << '\n';
      }
      SendAll(fd,req.str());
      for (auto& w:work_) {
        auto reply=ReadLine(fd);
        std::istringstream ss(reply);
        float q=0,d=0;
        if(!(ss >> q >> d)||!std::isfinite(q)||!std::isfinite(d)||
           q < -1.00001f||q > 1.00001f)
          throw std::runtime_error("Invalid ChessGPT bridge Q/D response: "+reply);
        if (w.out.q) *w.out.q=q;
        if (w.out.d) *w.out.d=d;
        if (w.out.m) *w.out.m=0;
        if(w.out.p.size() != w.moves.size())
          throw std::runtime_error("Lc0 policy size mismatch");
        float sum=0;
        for (size_t i=0;i<w.moves.size();++i) {
          if (!(ss >> w.out.p[i]) || !std::isfinite(w.out.p[i])|| w.out.p[i]<0)
            throw std::runtime_error("Invalid ChessGPT policy output");
          sum+=w.out.p[i];
        }
        if (sum<=0) throw std::runtime_error("ChessGPT returned zero policy mass");
        for (auto& p:w.out.p) p/=sum;
      }
    }catch(...){close(fd);throw;}
    close(fd);
  }
 private:
  std::vector<Pending> work_;
};

class BridgeBackend final : public Backend {
 public:
  BackendAttributes GetAttributes() const override {
    return BackendAttributes{.has_mlh=false,.has_wdl=false,.runs_on_cpu=true,
      .suggested_num_search_threads=1,.recommended_batch_size=1,
      .maximum_batch_size=32};
  }
  std::unique_ptr<BackendComputation> CreateComputation() override {
    return std::make_unique<BridgeComputation>();
  }
};
class BridgeFactory final:public BackendFactory {
 public:
  int GetPriority() const override {return -50;}
  std::string_view GetName() const override {return "chessgpt_socket";}
  std::unique_ptr<Backend> Create(const OptionsDict&) override {
    return std::make_unique<BridgeBackend>();
  }
};
[[maybe_unused]] static BackendManager::Register reg_chessgpt(
    std::make_unique<BridgeFactory>());
} // namespace
} // namespace lczero
